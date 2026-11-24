"""Explicit account-scoped ranking of current Code declaration excerpts."""

import asyncio
import hashlib
import json
import os
import uuid
from collections import OrderedDict
from pathlib import PurePosixPath

CONSENT_KEY = "code_vector_search_consent"
ROUTE_FIELDS = ("route_id", "connection_id", "provider", "model", "route_fingerprint")
MAX_DECLARATIONS = 6
MAX_SOURCE_BYTES = 128 * 1024
MAX_EXCERPT_BYTES = 8192
MAX_DIMENSION = 1024
_CACHE_ENTRIES = 128
_CACHE_TTL_SECONDS = 5 * 60
_cache = OrderedDict()


def clear_account_vectors(account):
    """Drop derived page/query vectors when the named account changes consent."""
    for key in tuple(_cache):
        if key[1] == account:
            _cache.pop(key)[3].cancel()


def _read_vector(key, now):
    item = _cache.get(key)
    if item is None:
        return None
    if item[0] <= now:
        _cache.pop(key)[3].cancel()
        return None
    _cache.move_to_end(key)
    return item[1:3]


def _save_vector(key, now, descriptor, values):
    prior = _cache.pop(key, None)
    if prior is not None:
        prior[3].cancel()
    expiration = now + _CACHE_TTL_SECONDS
    def expire():
        if (item := _cache.get(key)) is not None and item[0] == expiration:
            _cache.pop(key)
    timer = asyncio.get_running_loop().call_at(expiration, expire)
    _cache[key] = (expiration, descriptor.copy(), tuple(values), timer)
    while len(_cache) > _CACHE_ENTRIES:
        _cache.popitem(last=False)[1][3].cancel()


def _source_key(scope, hit, source):
    span = source["span"]
    return (*scope, "declaration", hit["id"], source["file_id"], source["source_hash"],
            span["start_byte"], span["end_byte"])


def _operation(owner, texts, root, key, route_id=None):
    from src.openclank.operation_router import ManagedOperationRequest
    return ManagedOperationRequest(
        owner=owner, operation="embeddings.create", purpose="embeddings",
        input={"texts": texts, "purpose": "index"}, model_route_id=route_id,
        root_operation_id=root, idempotency_key=key,
    )


def _route(prepared):
    if len(prepared.routes) != 1:
        raise ValueError("One configured embedding route is required")
    from src.openclank.provider_store import ProviderStore

    route = prepared.routes[0]
    store = ProviderStore()
    connection = store.get_connection(owner=prepared.owner, connection_id=route.connection_id)
    model = store.get_model_route(owner=prepared.owner, model_route_id=route.model_route_id)
    if (not connection.enabled or not model.enabled or connection.deleted_at is not None
            or model.deleted_at is not None or model.connection_id != connection.id
            or model.provider_model_id != route.model_id or connection.family_id != route.provider_id):
        raise ValueError("Embedding route changed")
    fingerprint = hashlib.sha256(repr((connection.id, connection.revision, connection.normalized_url,
        connection.adapter_id, model.id, model.revision, model.catalog_revision)).encode()).hexdigest()
    return {"route_id": route.model_route_id, "connection_id": route.connection_id,
            "provider": route.provider_id, "model": route.model_id, "route_fingerprint": fingerprint}


def consent_preview(owner):
    """Resolve the current account's route without issuing a model request."""
    from src.openclank.operation_router import get_operation_router
    root = f"code_vector_consent_{uuid.uuid4().hex}"
    return _route(get_operation_router().prepare(_operation(owner, ["route readiness"], root, root)))


def read_consent(prefs, account):
    value = prefs.get(CONSENT_KEY)
    keys = {"version", "account", "consent_epoch", *ROUTE_FIELDS}
    if (type(value) is dict and set(value) == keys and type(value["version"]) is int
            and value["version"] == 1 and value["account"] == account
            and all(type(value[key]) is str and 0 < len(value[key]) <= 512
                    for key in keys - {"version"})):
        return value.copy()
    return None


def disclosure():
    return {
        "max_excerpts": MAX_DECLARATIONS, "max_excerpt_bytes": MAX_EXCERPT_BYTES,
        "inputs": "Search text and UTF-8 source excerpts from a bounded declaration page in the active workspace. If no declaration matches lexically, the first page can include other admitted declarations.",
        "storage": "Page vectors expire from memory after five minutes. Reviewed index batches can also save private declaration vectors for the current workspace generation. Search can use that index when the embedding model still matches.",
    }


async def rerank_code_page(*, baseline, query, binding, repository, provider, auth_manager, session, purpose, current_binding):
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    from src.embeddings import ManagedEmbeddingClient
    from src.memery_provider import MemeryProvider
    from src.openclank.code_query import SCHEMA, CodeQueryError, _files_client, _relative, _source_guard, measure_envelope
    from src.openclank.operation_router import get_operation_router

    def unavailable(reason):
        baseline["degraded"] = True
        reasons = baseline.setdefault("reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        return baseline

    def fit(ranked):
        # Reranking adds evidence to a finite public metadata envelope.
        # Drop only trailing hits and preserve the lexical page on exhaustion.
        hits = ranked.get("hits", [])
        while hits:
            try:
                return measure_envelope(ranked, query["budgets"])
            except CodeQueryError:
                hits.pop()
                retained = {hit["id"] for hit in hits}
                ranked["edges"] = [edge for edge in ranked.get("edges", [])
                                   if edge["from"] in retained and edge["to"] in retained]
                ranked["partial"] = True
                for count in ranked.get("counts", []):
                    if count.get("name") == "returned":
                        count["value"] = len(hits)
        try:
            return measure_envelope(ranked, query["budgets"])
        except CodeQueryError:
            return unavailable("code_vector_ranking_envelope_budget")

    if (type(provider) is not MemeryProvider
            or not (query.get("query") or "").strip() or "source" not in query["fields"]
            or (session is not None and getattr(session, "incognito", False))):
        return unavailable("code_vector_search_unavailable")
    owner, account = binding["owner"], auth_manager.account_id(binding["owner"])
    if not account or auth_manager.username_for_account_id(account) != owner:
        return unavailable("code_vector_search_unavailable")
    with PREFERENCE_LOCK:
        consent = read_consent(_load_for_user(owner), account)
    if consent is None:
        return unavailable("code_vector_search_opt_in_required")
    receipt = baseline.get("receipt")
    if not isinstance(receipt, dict) or baseline.get("error"):
        return unavailable("code_vector_index_unavailable")
    selected = [hit for hit in baseline.get("hits", [])
                if hit.get("kind") == "declaration" and isinstance(hit.get("source"), dict)]

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 12.0
    cost = baseline.setdefault("cost", {})
    args = dict(binding=binding, repository=repository, provider=provider, auth_manager=auth_manager,
                session=session, purpose=purpose, expected=receipt, cost=cost)

    def current():
        try:
            if (loop.time() >= deadline or current_binding() is not True
                    or auth_manager.account_id(owner) != account
                    or auth_manager.username_for_account_id(account) != owner):
                return False
            with PREFERENCE_LOCK:
                return read_consent(_load_for_user(owner), account) == consent
        except Exception:
            return False

    excerpts = []
    cache_scope = (id(provider), account, owner, binding["workspace_id"], binding["policy_generation"],
                   consent["consent_epoch"], consent["route_fingerprint"],
                   json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    query_key = (*cache_scope, "query", hashlib.sha256(query["query"].encode()).hexdigest())
    try:
        async with asyncio.timeout_at(deadline):
            fence = await _source_guard(**args, captured_fence=None)
            # A compatible complete artifact uses only the explicit search
            # text. The search cannot start or advance its build.
            filters = {name: value for name, value in query.get("filter", {}).items() if value}
            if binding["origin"] == "app" and query.get("cursor") is None and filters in ({}, {"kinds": ["declaration"]}):
                index_scope = {"receipt": receipt, "consent_epoch": consent["consent_epoch"],
                               "route_fingerprint": consent["route_fingerprint"]}
                try:
                    index_status = await provider.code_query(binding=binding, query=None, _code_vector_index={
                        "scope": index_scope, "action": {"action": "status"},
                    })
                except Exception:
                    index_status = {}
                status_cost = (index_status.get("cost") or {}).get("native_rpc_calls_completed", 0)
                if type(status_cost) is int and status_cost >= 0:
                    cost["native_rpc_calls_completed"] = cost.get("native_rpc_calls_completed", 0) + status_cost
                if (index_status.get("state") == "ready" and type(index_status.get("indexed")) is int
                        and index_status["indexed"] > 0 and current()):
                    root = f"code_vector_search_{uuid.uuid4().hex}"
                    key = hashlib.sha256(root.encode()).hexdigest()
                    router = get_operation_router()
                    inputs = [query["query"]]
                    prepared = await asyncio.to_thread(router.prepare, _operation(owner, inputs, root, key, consent["route_id"]))
                    expected_route = {field: consent[field] for field in ROUTE_FIELDS}

                    def index_current():
                        return current() and _route(prepared) == expected_route

                    if not index_current():
                        return unavailable("code_vector_route_changed")
                    cached = _read_vector(query_key, loop.time())
                    if cached is None:
                        client = ManagedEmbeddingClient(owner=owner, model_route_id=consent["route_id"], root_operation_id=root)
                        encoded = await client._encode_owned(inputs, idempotency_key=key, deadline=deadline,
                                                            before_issue=index_current, prepared_original=prepared)
                        if len(encoded.shape) != 2 or encoded.shape[0] != 1 or not 1 <= encoded.shape[1] <= MAX_DIMENSION:
                            return unavailable("code_vector_profile_unavailable")
                        descriptor = {"provider": client.provider_ref, "model": client.model,
                                      "revision": client.model_fingerprint, "dimension": int(encoded.shape[1])}
                        query_vector = encoded[0].tolist()
                    else:
                        descriptor, query_vector = cached
                    await _source_guard(**args, captured_fence=fence)
                    if not index_current():
                        return unavailable("code_vector_admission_changed")
                    ranked = await provider.code_query(binding=binding, query={**query, "expected_receipt": receipt},
                        _admitted_code_vectors={
                            "owner": owner, "owner_subject_id": binding["owner_subject_id"],
                            "indexed_receipt": receipt, "profile": descriptor, "query_profile": descriptor,
                            "query": query_vector, "declarations": [], "published": index_scope,
                        })
                    if not index_current() or ranked.get("receipt") != receipt or ranked.get("error"):
                        return unavailable("code_vector_admission_changed")
                    if "semantic_ranked_published_generation" in ranked.get("reasons", ()):
                        _save_vector(query_key, loop.time(), descriptor, query_vector)
                    native_cost = ranked.setdefault("cost", {})
                    for field in ("native_rpc_calls_completed", "source_fence_checks_completed"):
                        native_cost[field] = native_cost.get(field, 0) + cost.get(field, 0)
                    return fit(ranked)
            if not selected and query.get("cursor") is None:
                spare = query["budgets"]["rows"] - len(baseline.get("hits", []))
                work = query["budgets"]["work"] - cost.get("inspected_records", 0)
                if spare > 0 and work > 0:
                    outline = await provider.code_query(binding=binding, query={
                        "schema": SCHEMA, "operation": "outline",
                        "fields": ["label", "source"], "channels": [], "filter": query["filter"],
                        "expected_receipt": receipt,
                        "budgets": {**query["budgets"], "rows": min(spare, MAX_DECLARATIONS), "work": work},
                    })
                    if not outline.get("error") and outline.get("receipt") == receipt:
                        selected = [hit for hit in outline.get("hits", [])
                                    if hit.get("kind") == "declaration" and isinstance(hit.get("source"), dict)]
                    for field in ("metadata_bytes", "inspected_records", "native_rpc_calls_completed"):
                        amount = (outline.get("cost") or {}).get(field, 0)
                        if type(amount) is int and amount >= 0:
                            cost[field] = cost.get(field, 0) + amount
            if not selected:
                return unavailable("code_vector_no_matching_declarations")
            files = _files_client(binding=binding, repository=repository, auth_manager=auth_manager,
                                  session=session, purpose=purpose)
            root = f"code_vector_search_{uuid.uuid4().hex}"
            key = hashlib.sha256(root.encode()).hexdigest()
            for hit in selected[:MAX_DECLARATIONS]:
                if not current():
                    return unavailable("code_vector_admission_changed")
                source = hit["source"]
                span = source.get("span") or {}
                start, end = span.get("start_byte"), span.get("end_byte")
                if type(start) is not int or type(end) is not int or not 0 <= start < end or end-start > MAX_EXCERPT_BYTES:
                    continue
                rel = _relative(source.get("rel_path"))
                path = os.path.join(binding["root"], *PurePosixPath(rel).parts)
                opened = await files.open_handle(path)
                handle, tag, size = opened.get("handle"), opened.get("object_tag"), opened.get("size")
                if (type(handle) is not dict or type(tag) is not str or not tag or type(size) is not int
                        or not end <= size <= MAX_SOURCE_BYTES):
                    continue
                response = await files.request_handle("read_range", handle, {
                    "offset": start, "length": end-start, "include_fingerprint": False,
                })
                data = response.get("data") or {}
                raw = data.get("bytes")
                if type(raw) is not list or len(raw) != end-start or data.get("offset") != start:
                    continue
                text = bytes(raw).decode("utf-8", errors="strict")
                if not text.strip():
                    continue
                cost["files_bytes_read"] = cost.get("files_bytes_read", 0) + len(raw)
                claim = {"source": source, "predicate": {"kind": "exact_text", "text": text}}
                checked = await provider.code_query(binding=binding, source_claim=claim, query={
                    "schema": SCHEMA, "operation": "capabilities", "fields": [], "channels": [],
                    "expected_receipt": receipt, "budgets": query["budgets"],
                })
                for field in ("validation_bytes_read", "native_rpc_calls_completed"):
                    count = (checked.get("cost") or {}).get(field, 0)
                    if type(count) is int and count >= 0:
                        cost[field] = cost.get(field, 0) + count
                if (checked.get("receipt") == receipt
                        and (checked.get("source_validation") or {}).get("state") == "current"):
                    excerpts.append((hit["id"], source, text, path, tag, size,
                                     _source_key(cache_scope, hit, source)))
            if not excerpts:
                return unavailable("code_vector_current_excerpts_unavailable")
            await _source_guard(**args, captured_fence=fence)
            if not current():
                return unavailable("code_vector_admission_changed")
            now = loop.time()
            cached_query = _read_vector(query_key, now)
            cached = {row[6]: _read_vector(row[6], now) for row in excerpts}
            missing = [row for row in excerpts
                       if cached_query is None or cached[row[6]] is None or cached[row[6]][0] != cached_query[0]]
            reusable = cached_query is not None and not missing
            # Without a complete compatible cache hit the exact route encodes
            # the query alongside the needed source excerpts, once.
            texts = ["route readiness"] if reusable else [query["query"], *(row[2] for row in missing)]
            router = get_operation_router()
            prepared = await asyncio.to_thread(router.prepare, _operation(owner, texts, root, key, consent["route_id"]))
            selected_route = {field: consent[field] for field in ROUTE_FIELDS}
            if _route(prepared) != selected_route:
                return unavailable("code_vector_route_changed")

            def route_current():
                return current() and _route(prepared) == selected_route

            if reusable:
                descriptor, query_vector = cached_query
            else:
                client = ManagedEmbeddingClient(owner=owner, model_route_id=consent["route_id"], root_operation_id=root)
                vectors = await client._encode_owned(texts, idempotency_key=key, deadline=deadline,
                                                     before_issue=route_current, prepared_original=prepared)
                if len(vectors.shape) != 2 or vectors.shape[0] != len(texts) or not 1 <= vectors.shape[1] <= MAX_DIMENSION:
                    return unavailable("code_vector_profile_unavailable")
                descriptor = {"provider": client.provider_ref, "model": client.model,
                              "revision": client.model_fingerprint, "dimension": int(vectors.shape[1])}
                query_vector = vectors[0].tolist()
                for row, vector in zip(missing, vectors[1:]):
                    cached[row[6]] = (descriptor, vector.tolist())
            for _, _, _, path, tag, size, _ in excerpts:
                reopened = await files.open_handle(path)
                if reopened.get("object_tag") != tag or reopened.get("size") != size:
                    return unavailable("code_vector_source_changed")
            await _source_guard(**args, captured_fence=fence)
            if not route_current():
                return unavailable("code_vector_admission_changed")
            admitted = [{"declaration_id": row[0], "source": row[1], "values": values}
                        for row in excerpts if (entry := cached[row[6]]) is not None
                        for profile, values in (entry,) if profile == descriptor]
            if not admitted:
                return unavailable("code_vector_profile_unavailable")
            ranked = await provider.code_query(binding=binding, query={**query, "expected_receipt": receipt},
                _admitted_code_vectors={
                    "owner": owner, "owner_subject_id": binding["owner_subject_id"],
                    "indexed_receipt": receipt, "profile": descriptor, "query_profile": descriptor,
                    "query": query_vector, "declarations": admitted,
                })
            if not route_current() or ranked.get("receipt") != receipt or ranked.get("error"):
                return unavailable("code_vector_admission_changed")
            if not reusable and "semantic_ranked_current_page" in ranked.get("reasons", ()):
                now = loop.time()
                _save_vector(query_key, now, descriptor, query_vector)
                for row in missing:
                    _save_vector(row[6], now, *cached[row[6]])
            native_cost = ranked.get("cost", {})
            for field in ("files_bytes_read", "validation_bytes_read", "native_rpc_calls_completed", "source_fence_checks_completed"):
                amount = cost.get(field, 0)
                if type(amount) is int and amount >= 0:
                    native_cost[field] = native_cost.get(field, 0) + amount
            return fit(ranked)
    except asyncio.CancelledError:
        raise
    except Exception:
        return unavailable("code_vector_search_unavailable")

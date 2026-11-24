"""Owner-reviewed batches for a durable, generation-scoped Code vector index."""

import asyncio
import hashlib
import json
import os
import uuid
from pathlib import PurePosixPath

from src.openclank.code_vector_retrieval import (
    MAX_DECLARATIONS,
    MAX_DIMENSION,
    MAX_EXCERPT_BYTES,
    MAX_SOURCE_BYTES,
    ROUTE_FIELDS,
    _operation,
    _route,
    read_consent,
)

_STATES = {"unavailable", "partial", "ready", "pending"}
_PUBLIC_COUNTS = ("indexed", "visited", "omitted")


def _state(value):
    state = value.get("state")
    if value.get("error") or state not in _STATES:
        return {"state": "unavailable", **dict.fromkeys(_PUBLIC_COUNTS, 0), "can_continue": False}
    counts = {name: value[name] for name in _PUBLIC_COUNTS}
    if any(type(count) is not int or count < 0 for count in counts.values()):
        return _state({})
    return {"state": state, **counts, "can_continue": value.get("can_continue") is True}


def valid_request(body):
    if type(body) is not dict:
        return False
    action, workspace, batch = (body.get(key) for key in ("action", "workspace_id", "expected_batch"))
    required = {"action", "workspace_id"} | ({"expected_batch"} if action == "build" else set())
    return (set(body) == required and action in ("status", "preview", "build")
            and type(workspace) is str and 0 < len(workspace) <= 256
            and (action != "build" or (type(batch) is str and len(batch) == 64
                 and all(c in "0123456789abcdef" for c in batch))))


def _profile(client, embedded):
    return {"provider": client.provider_ref, "model": client.model,
            "revision": client.model_fingerprint, "dimension": int(embedded.shape[1])}


def _span(source):
    span = source.get("span") or {}
    start, end = span.get("start_byte"), span.get("end_byte")
    if type(start) is int and type(end) is int and 0 <= start < end <= MAX_SOURCE_BYTES and end-start <= MAX_EXCERPT_BYTES:
        return start, end
    return None


async def _read(files, binding, provider, query, slot):
    from src.openclank.code_query import _relative
    source = slot["source"]
    span = _span(source)
    if span is None:
        return None
    start, end = span
    rel = _relative(source["rel_path"])
    path = os.path.join(binding["root"], *PurePosixPath(rel).parts)
    opened = await files.open_handle(path)
    handle, tag, size = opened.get("handle"), opened.get("object_tag"), opened.get("size")
    if (type(handle) is not dict or type(tag) is not str or not tag or type(size) is not int
            or not end <= size <= MAX_SOURCE_BYTES):
        return None
    response = await files.request_handle("read_range", handle, {
        "offset": start, "length": end-start, "include_fingerprint": False,
    })
    data = response.get("data") or {}
    raw = data.get("bytes")
    if type(raw) is not list or len(raw) != end-start or data.get("offset") != start:
        return None
    text = bytes(raw).decode("utf-8", errors="strict")
    if not text.strip():
        return None
    checked = await provider.code_query(binding=binding, query=query, source_claim={
        "source": source, "predicate": {"kind": "exact_text", "text": text},
    })
    if (checked.get("receipt") != query["expected_receipt"]
            or (checked.get("source_validation") or {}).get("state") != "current"):
        return None
    return slot, text, path, tag, size


async def manage_code_vectors(*, state, owner, account, original_current, body):
    from routes.prefs_routes import PREFERENCE_LOCK, _load_for_user
    from src.embeddings import ManagedEmbeddingClient
    from src.memery_provider import MemeryProvider
    from src.openclank.chat_source_bridge import canonical_source_provider
    from src.openclank.code_query import DEFAULT_BUDGETS, SCHEMA, _files_client, resolve_query_binding
    from src.openclank.operation_router import get_operation_router

    unavailable = _state({})
    action, workspace = body["action"], body["workspace_id"]
    auth = getattr(state, "auth_manager", None)
    repository = getattr(state, "files_policy_repository", None)
    deadline = asyncio.get_running_loop().time() + 30
    reserved = None
    try:
        async with asyncio.timeout_at(deadline):
            provider = canonical_source_provider(state)
            if type(provider) is not MemeryProvider or auth is None or repository is None:
                return unavailable
            with PREFERENCE_LOCK:
                consent = read_consent(_load_for_user(owner), account)
            if consent is None or original_current() is not True:
                return unavailable

            def resolve():
                return resolve_query_binding(repository=repository, auth_manager=auth, owner=owner,
                                             workspace_id=workspace, purpose="app_folder")

            binding, identity = resolve()
            routes = {field: consent[field] for field in ROUTE_FIELDS}

            def current():
                try:
                    if (asyncio.get_running_loop().time() >= deadline or original_current() is not True
                            or state.auth_manager is not auth or state.files_policy_repository is not repository
                            or canonical_source_provider(state) is not provider or resolve() != (binding, identity)):
                        return False
                    with PREFERENCE_LOCK:
                        return read_consent(_load_for_user(owner), account) == consent
                except Exception:
                    return False

            query = {"schema": SCHEMA, "operation": "capabilities", "fields": [], "channels": [],
                     "budgets": DEFAULT_BUDGETS}
            caps = await provider.code_query(binding=binding, query=query)
            receipt = caps.get("receipt")
            if not current() or caps.get("error") or not isinstance(receipt, dict):
                return unavailable
            query = {**query, "expected_receipt": receipt}
            scope = {"receipt": receipt, "consent_epoch": consent["consent_epoch"],
                     "route_fingerprint": consent["route_fingerprint"]}

            async def native(code, **kwargs):
                return await provider.code_query(binding=binding, query=None, _code_vector_index={
                    "scope": scope, "action": {"action": code, **kwargs},
                })

            preview = await native("status" if action == "status" else "preview")
            result = _state(preview)
            if not current():
                return unavailable
            if result["state"] == "unavailable":
                return result
            pending = preview.get("pending") if result["state"] == "pending" else None
            original = pending["original"] if pending else uuid.uuid4().hex
            root = "code_vector_build_" + original
            key = hashlib.sha256(root.encode()).hexdigest()
            router = get_operation_router()

            async def prepare(texts):
                return await asyncio.to_thread(router.prepare, _operation(owner, texts, root, key, consent["route_id"]))

            readiness = await prepare(["route readiness"])
            if not current() or _route(readiness) != routes:
                return unavailable
            if action == "status" and pending:
                reserved = result
                slots = {slot["declaration_id"]: slot for slot in pending["slots"]}
                files = _files_client(binding=binding, repository=repository, auth_manager=auth,
                                      session=None, purpose="app_folder")
                recovered_rows = []
                for identifier in pending["sent_ids"]:
                    row = await _read(files, binding, provider, query, slots[identifier])
                    if row is None or not current():
                        return reserved
                    recovered_rows.append(row)
                profile, vectors = None, []
                if recovered_rows:
                    prepared = await prepare([row[1] for row in recovered_rows])
                    def route_current():
                        return current() and _route(prepared) == routes
                    if not route_current():
                        return reserved
                    outcome = await asyncio.to_thread(router.recover_prepared, prepared, authenticated_owner=owner)
                    if outcome.get("state") != "terminal" or not route_current():
                        return reserved
                    client = ManagedEmbeddingClient(owner=owner, model_route_id=consent["route_id"], root_operation_id=root)
                    embedded = client._accept_owned_terminal(outcome["result"], expected_count=len(recovered_rows),
                                                              before_issue=route_current)
                    if not 1 <= embedded.shape[1] <= MAX_DIMENSION:
                        return reserved
                    profile = _profile(client, embedded)
                    for slot, _, path, tag, size in recovered_rows:
                        reopened = await files.open_handle(path)
                        if reopened.get("object_tag") != tag or reopened.get("size") != size:
                            return reserved
                    vectors = [{**row[0], "values": vector.tolist()} for row, vector in zip(recovered_rows, embedded)]
                if not current():
                    return unavailable
                completed = await native("settle", original=original, profile=profile, vectors=vectors)
                return _state(completed) if current() and not completed.get("error") else reserved
            if action == "status" or not result["can_continue"]:
                return result
            page = preview["page"]
            slots, reusable = page["slots"], set(page["reusable"])
            if type(slots) is not list or len(slots) > MAX_DECLARATIONS:
                return unavailable
            eligible = [slot for slot in slots if slot["declaration_id"] not in reusable and _span(slot["source"])]
            cost = {"excerpts": len(eligible), "max_source_bytes": sum(
                _span(slot["source"])[1] - _span(slot["source"])[0] for slot in eligible),
                "remaining_estimate": None}
            # Compare original consent, native scope, exact authored page, and cost on Build.
            batch_id = hashlib.sha256(json.dumps((account, scope, preview.get("cursor"), page, cost),
                                                  sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            if action == "preview":
                return {**result, "batch_id": batch_id, "cost": cost,
                        "disclosure": "One reviewed batch can send the displayed amount of declaration source to the selected embedding route. Current compatible excerpts are reused locally."}
            if batch_id != body["expected_batch"]:
                return {**result, "batch_changed": True}
            files = _files_client(binding=binding, repository=repository, auth_manager=auth,
                                  session=None, purpose="app_folder")
            excerpts = []
            for slot in eligible:
                if not current():
                    return unavailable
                listed = await _read(files, binding, provider, query, slot)
                if listed is not None:
                    excerpts.append(listed)
            for _, _, path, tag, size in excerpts:
                reopened = await files.open_handle(path)
                if reopened.get("object_tag") != tag or reopened.get("size") != size:
                    return unavailable
            texts = [row[1] for row in excerpts]
            prepared = await prepare(texts) if texts else readiness

            def route_current():
                return current() and _route(prepared) == routes

            if not route_current():
                return unavailable
            start = await native("reserve", cursor=preview.get("cursor"), next_cursor=page.get("next_cursor"),
                                 original=original, slots=slots,
                                 sent_ids=[row[0]["declaration_id"] for row in excerpts])
            if start.get("error") or start.get("state") != "pending":
                return _state(await native("status")) if current() else unavailable
            reserved = _state(start)

            async def undo_undispatched():
                if not current():
                    return unavailable
                cleared = await native("reset_undispatched", original=original)
                return reserved if cleared.get("error") or not current() else _state(cleared)

            for _, _, path, tag, size in excerpts:
                reopened = await files.open_handle(path)
                if reopened.get("object_tag") != tag or reopened.get("size") != size:
                    return await undo_undispatched()
            if not route_current():
                return await undo_undispatched()
            profile, vectors = None, []
            if texts:
                client = ManagedEmbeddingClient(owner=owner, model_route_id=consent["route_id"], root_operation_id=root)
                embedded = await client._encode_owned(texts, idempotency_key=key, deadline=deadline,
                                                       before_issue=route_current, prepared_original=prepared)
                if len(embedded.shape) != 2 or embedded.shape[0] != len(texts) or not 1 <= embedded.shape[1] <= MAX_DIMENSION:
                    return reserved
                profile = _profile(client, embedded)
                vectors = [{**row[0], "values": vector.tolist()} for row, vector in zip(excerpts, embedded)]
                for _, _, path, tag, size in excerpts:
                    reopened = await files.open_handle(path)
                    if reopened.get("object_tag") != tag or reopened.get("size") != size:
                        return reserved
            if not route_current():
                return reserved
            complete = await native("settle", original=original, profile=profile, vectors=vectors)
            if not current() or complete.get("error"):
                return reserved
            return _state(complete)
    except asyncio.CancelledError:
        raise
    except Exception:
        return reserved or unavailable

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
import secrets
from collections import OrderedDict
from pathlib import PurePosixPath

SCHEMA = "open-clank.code-query/v1"
OPERATIONS = frozenset("capabilities search overview catalog facets outline inspect neighbors path impact read pipeline".split())
CHANNELS = frozenset("exact identifier fuzzy lexical structural raw semantic".split())
FIELDS = frozenset("label source signature documentation occurrences resolution properties excerpt witnesses".split())
MAX_QUERY_WORK = 4_000_000
DEFAULT_BUDGETS = dict(rows=50, bytes=65536, tokens=8192, hops=4, work=MAX_QUERY_WORK)
LIMITS = dict(rows=(1, 200), bytes=(512, 262144), tokens=(512, 8192), hops=(0, 8), work=(1, MAX_QUERY_WORK))


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
        **{key: {"type": "array", "maxItems": 64, "items": {"type": "string", "maxLength": 128}} for key in ("kinds", "symbol_kinds", "languages", "states")}}}
    receipt = {"type": "object", "additionalProperties": False, "required": ["workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "admission_epoch", "policy_generation", "config_fingerprint"],
        "properties": {**{k: {"type": "string", "minLength": 1, "maxLength": 256} for k in ("workspace_id", "registration_id", "source_snapshot_id", "checkout_id", "head_generation", "config_fingerprint")},
            "admission_epoch": {"type": "integer", "minimum": 0}, "policy_generation": {"type": "integer", "minimum": 0}}}
    return {"type": "function", "function": {"name": "search_code", "description": "Read-only authorized active Workspace code: exact/identifier/metadata lexical search, conservative typo suggestions, catalog/outline, witnessed neighbors, incoming impact and directed paths. Explicit relative read windows use the existing Files owner. Search with channels=[raw] and no read descriptor performs bounded case-sensitive literal Source contents search of current admitted catalog file bytes; new paths require ordinary index refresh, partial results are explicit, no body FTS or source-proof claim. Metadata is not current source proof; semantic capability is unavailable. No owner/root/index/erase inputs.",
        "parameters": {"type": "object", "additionalProperties": False, "properties": {
            "schema": {"type": "string", "enum": [SCHEMA]},
            "operation": {"type": "string", "enum": sorted(OPERATIONS - {"pipeline"}), "default": "search"},
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
            "request_id": {"type": "string", "maxLength": 128}}}}}


def _relative(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or "\0" in value or "\\" in value or value.startswith("/") or any(p in {"", ".", ".."} for p in value.rstrip("/").split("/")):
        raise CodeQueryError("invalid", "Invalid relative code resource")
    return value


def validate_query(query):
    if not isinstance(query, dict) or set(query) - {"schema", "operation", "query", "seed_ids", "target_id", "channels", "fields", "filter", "budgets", "expected_receipt", "read", "direction", "cursor", "stages", "request_id"}:
        raise CodeQueryError("invalid", "Invalid code query fields")
    q = copy.deepcopy(query)
    q.setdefault("schema", SCHEMA)
    q.setdefault("operation", "search")
    if q["schema"] != SCHEMA or not isinstance(q["operation"], str) or q["operation"] not in OPERATIONS:
        raise CodeQueryError("invalid", "Unsupported code operation")
    if q["operation"] == "pipeline" or q.get("stages"):
        raise CodeQueryError("unsupported", "Composed pipelines are optional and not admitted")
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
    for key, maximum in (("query", 1024), ("cursor", 4096), ("request_id", 128)):
        if q.get(key) is not None and (not isinstance(q[key], str) or len(q[key].encode()) > maximum):
            raise CodeQueryError("invalid", "Invalid code query text")
    ids = q.setdefault("seed_ids", [])
    if not isinstance(ids, list) or len(ids) > 200:
        raise CodeQueryError("invalid", "Invalid code seeds")
    for value in ids + ([q["target_id"]] if q.get("target_id") is not None else []):
        if not isinstance(value, str) or not value.startswith(("file:", "decl:", "occ:")) or not 1 <= len(value.split(":", 1)[1]) <= 128 or any(c not in "0123456789abcdefABCDEF" for c in value.split(":", 1)[1]):
            raise CodeQueryError("invalid", "Expected typed code identity")
    if q.get("direction") is not None and (not isinstance(q["direction"], str) or q["direction"] not in {"incoming", "outgoing", "both"}):
        raise CodeQueryError("invalid", "Invalid code direction")
    stages = q.setdefault("stages", [])
    if not isinstance(stages, list) or len(stages) > 8:
        raise CodeQueryError("invalid", "Too many code stages")
    read_seen = False
    for index, stage in enumerate(stages):
        if not isinstance(stage, dict) or set(stage) - {"operation", "query", "filter", "with_stage", "limit"} or stage.get("operation") not in {"find", "filter", "deps", "outline", "read", "limit", "union", "intersection", "difference"}:
            raise CodeQueryError("invalid", "Invalid code stage")
        if read_seen and stage["operation"] != "limit":
            raise CodeQueryError("invalid", "Read must be terminal except limit")
        read_seen |= stage["operation"] == "read"
        if stage.get("query") is not None and (not isinstance(stage["query"], str) or len(stage["query"].encode()) > 1024):
            raise CodeQueryError("invalid", "Invalid stage text")
        _filter(stage.get("filter", {}))
        if stage.get("with_stage") is not None and (type(stage["with_stage"]) is not int or not 0 <= stage["with_stage"] < index):
            raise CodeQueryError("invalid", "Set operation needs a previous stage")
        if stage.get("limit") is not None and (type(stage["limit"]) is not int or not 0 <= stage["limit"] <= 200):
            raise CodeQueryError("invalid", "Invalid stage limit")
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


def _filter(value):
    if not isinstance(value, dict) or set(value) - {"rel_path", "path_prefix", "kinds", "symbol_kinds", "languages", "states"}:
        raise CodeQueryError("invalid", "Invalid code filter")
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


async def query_code(*, repository, provider, auth_manager, owner, workspace_id, purpose, session=None, query):
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
        if wants_contents:
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
            result = await invoke(binding=native_binding, query=native_query)
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
            if wants_read:
                result = await _read_source(result=result, query=q, binding=native_binding, repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose)
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


async def _source_guard(*, binding, repository, provider, auth_manager, session, purpose, expected, captured_fence, cost):
    from src.openclank.workspace_policy_service import resolve_owned_workspace, WorkspacePolicyServiceError
    if purpose == "agent_workspace" and (session is None or getattr(session, "owner", None) != binding["owner"] or getattr(session, "workspace_id", None) != binding["workspace_id"] or getattr(session, "archived", False)):
        raise CodeQueryError("denied", "Code chat binding changed")
    try:
        current = resolve_owned_workspace(repository, workspace_id=binding["workspace_id"], owner_username=binding["owner"], auth_manager=auth_manager, purpose=purpose)
    except WorkspacePolicyServiceError:
        raise CodeQueryError("denied", "Code source authority unavailable") from None
    if (current.workspace.owner_subject_id != binding["owner_subject_id"] or current.workspace.location_id != binding["location_id"] or current.workspace.revision != binding["workspace_revision"] or current.path != binding["root"] or repository.generation() != binding["policy_generation"]):
        raise CodeQueryError("denied", "Code source binding changed")
    fence = await provider.code_read_fence(binding=binding, expected_receipt=expected)
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


async def _read_source(*, result, query, binding, repository, provider, auth_manager, session, purpose):
    read = query["read"]
    rel = _relative(read["rel_path"])
    expected = result.get("receipt")
    cost = result.setdefault("cost", {})
    captured_fence = None
    async def guard():
        nonlocal captured_fence
        captured_fence = await _source_guard(binding=binding, repository=repository, provider=provider, auth_manager=auth_manager, session=session, purpose=purpose, expected=expected, captured_fence=captured_fence, cost=cost)
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
    return result

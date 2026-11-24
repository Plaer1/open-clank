"""Read-only SCIP index observations bound to independently current Files bytes."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import os
from pathlib import PurePosixPath
import re
import time

_MAX_INDEX = 512 * 1024
_MAX_DOCUMENT = 32768
_MAX_CANDIDATES = 6
_MAX_ACTIVE = 64
_ACTIVE_SECONDS = 15 * 60
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ACTIVE = OrderedDict()


class ScipUnavailable(ValueError):
    pass


@dataclass(frozen=True)
class Selection:
    receipt: dict
    relative_path: str
    digest: str
    expires: float


def _path(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 4096 or value[0] == "/" or "\\" in value or ":" in value:
        raise ScipUnavailable("invalid SCIP path")
    if any(part in ("", ".", "..") for part in value.split("/")) or any(ord(ch) < 32 for ch in value):
        raise ScipUnavailable("invalid SCIP path")
    return value


def _binding_key(binding):
    return tuple(binding[key] for key in ("owner_subject_id", "workspace_id", "workspace_revision", "policy_generation", "root"))


async def _read(client, binding, relative, limit):
    path = os.path.join(binding["root"], *PurePosixPath(_path(relative)).parts)
    entry = await client.open_handle(path)
    handle, size = entry.get("handle"), entry.get("size")
    if not isinstance(handle, dict) or type(size) is not int or not 0 < size <= limit:
        raise ScipUnavailable("Files object exceeds supported read bound")
    content = []
    for offset in range(0, size, 65536):
        length = min(size - offset, 65536)
        result = await client.request_handle("read_range", handle, {
            "offset": offset, "length": length, "include_fingerprint": False,
        })
        data = result.get("data") or {}
        raw = data.get("bytes")
        if not isinstance(raw, list) or len(raw) != length or data.get("offset") != offset or any(
            type(byte) is not int or not 0 <= byte <= 255 for byte in raw
        ):
            raise ScipUnavailable("incomplete Files read")
        content.append(bytes(raw))
    again = await client.open_handle(path)
    if not isinstance(entry.get("object_tag"), str) or any(entry.get(key) != again.get(key) for key in ("object_tag", "size", "modified_unix_ms")):
        raise ScipUnavailable("Files object changed")
    return b"".join(content)


async def _observe(provider, binding, receipt, data, source, cost):
    from src.openclank.code_compiler_evidence import _account
    response = await provider.code_query(binding=binding, query=None, _scip_request={
        "receipt": receipt, "index_base64": base64.b64encode(data).decode("ascii"), "source": source,
    })
    _account(cost, response)
    if (not isinstance(response, dict) or response.get("error") or response.get("receipt") != receipt
            or response.get("index_sha256") != hashlib.sha256(data).hexdigest()
            or not isinstance(response.get("producer"), dict)):
        raise ScipUnavailable("native SCIP evidence unavailable")
    return response


async def select_scip(*, owner, workspace, expected, resource_ref, repository, provider, auth_manager, current):
    from src.openclank.code_query import _files_client, _source_guard, resolve_query_binding, validate_query
    from src.openclank.files_host_provider import _path as host_origin_path
    from src.openclank.resource_refs import resolve_resource_ref

    validate_query({"operation": "capabilities", "expected_receipt": expected})
    if not current() or not isinstance(expected, dict) or expected.get("workspace_id") != workspace:
        raise ScipUnavailable("current workspace receipt required")
    binding, identity = resolve_query_binding(repository=repository, auth_manager=auth_manager, owner=owner,
                                               workspace_id=workspace, purpose="app_folder")
    key = _binding_key(binding)
    guard = dict(binding=binding, repository=repository, provider=provider, auth_manager=auth_manager,
                 session=None, purpose="app_folder", expected=expected, cost={})
    fence = await _source_guard(**guard, captured_fence=None)
    if resource_ref is None:
        if not current():
            raise ScipUnavailable("workspace changed")
        active = _ACTIVE.get(key)
        if active is not None and active.receipt == expected:
            _ACTIVE.pop(key, None)
        return {"state": "cleared"}
    ref = resolve_resource_ref(resource_ref, expected_owner_subject_id=binding["owner_subject_id"],
                               current_policy_generation=binding["policy_generation"], required_capability="read")
    if ref.provider != "host" or ref.kind not in ("file", "exact_file"):
        raise ScipUnavailable("select a Host SCIP file")
    path = host_origin_path(ref.origin_id)
    root = binding["root"]
    relative = _path(os.path.relpath(path, root).replace(os.sep, "/"))
    if not relative.endswith(".scip"):
        raise ScipUnavailable("SCIP binary index required")
    client = _files_client(binding=binding, repository=repository, auth_manager=auth_manager,
                           session=None, purpose="app_folder")
    data = await _read(client, binding, relative, _MAX_INDEX)
    evidence = await _observe(provider, binding, expected, data, None, {})
    producer = evidence["producer"]
    if await _read(client, binding, relative, _MAX_INDEX) != data:
        raise ScipUnavailable("SCIP file changed")
    await _source_guard(**guard, captured_fence=fence)
    if not current() or resolve_query_binding(repository=repository, auth_manager=auth_manager, owner=owner,
            workspace_id=workspace, purpose="app_folder")[1] != identity:
        raise ScipUnavailable("workspace changed")
    digest = hashlib.sha256(data).hexdigest()
    _ACTIVE[key] = Selection(expected.copy(), relative, digest, time.monotonic() + _ACTIVE_SECONDS)
    _ACTIVE.move_to_end(key)
    while len(_ACTIVE) > _MAX_ACTIVE:
        _ACTIVE.popitem(last=False)
    return {"state": "selected", "sha256": digest, "declared_producer": producer, "expires_in_seconds": _ACTIVE_SECONDS}


async def enrich_scip(*, result, query, binding, repository, provider, auth_manager, session, purpose):
    from src.openclank.code_compiler_evidence import _account, _verify_compiler_claim
    from src.openclank.files_service_client import FilesServiceError
    from src.openclank.code_query import SCHEMA, CodeQueryError, _files_client, _source_guard, _span, measure_envelope

    if (query["operation"] != "inspect" or query.get("cursor") is not None
            or not {"source", "properties"} <= set(query["fields"]) or len(query["seed_ids"]) != 1
            or not query["seed_ids"][0].startswith("occ:") or query.get("expected_receipt") is None
            or result.get("receipt") != query["expected_receipt"] or result.get("error") is not None):
        return
    key = _binding_key(binding)
    selection = _ACTIVE.get(key)
    if selection is None or selection.expires <= time.monotonic() or selection.receipt != result["receipt"]:
        return
    hits = result.get("hits", [])
    if len(hits) != 1 or hits[0].get("id") != query["seed_ids"][0] or hits[0].get("kind") != "occurrence":
        return
    source, properties = hits[0].get("source"), hits[0].get("properties")
    if not isinstance(source, dict) or not isinstance(properties, dict):
        return

    def publish(observation):
        properties["scipObservation"] = observation
        try:
            measure_envelope(result, query["budgets"])
        except CodeQueryError:
            properties.pop("scipObservation", None)

    cost = result.setdefault("cost", {})
    fence = None
    guard_args = dict(binding=binding, repository=repository, provider=provider, auth_manager=auth_manager,
                      session=session, purpose=purpose, expected=selection.receipt, cost=cost)

    async def guard():
        nonlocal fence
        if _ACTIVE.get(key) is not selection or selection.expires <= time.monotonic():
            raise ScipUnavailable("SCIP selection changed")
        fence = await _source_guard(**guard_args, captured_fence=fence)

    def native(operation, **extra):
        work = query["budgets"]["work"] - cost.get("inspected_records", 0)
        if work < 1:
            raise ScipUnavailable("SCIP work budget closed")
        return {**dict(schema=SCHEMA, operation=operation, expected_receipt=selection.receipt,
                       channels=[], fields=[], budgets={**query["budgets"], "work": work, "rows": 1}), **extra}

    async def verify(client, locator, document_sha256):
        await guard()
        body = await _read(client, binding, locator["rel_path"], _MAX_DOCUMENT)
        cost["files_bytes_read"] = cost.get("files_bytes_read", 0) + len(body)
        if (not isinstance(document_sha256, str) or not _DIGEST.fullmatch(document_sha256)
                or hashlib.sha256(body).hexdigest() != document_sha256):
            raise ScipUnavailable("SCIP document differs from current Files bytes")
        claim = {"source": {**locator, "span": _span(body, 0, len(body))},
                 "predicate": {"kind": "exact_text", "text": body.decode("utf-8")}}
        await _verify_compiler_claim(provider, binding, native("capabilities"), claim, cost)
        return body

    async def admit_target(client, found_address):
        relative = _path(found_address["path"])
        start, end, document_sha256 = (found_address[key] for key in ("start", "end", "document_sha256"))
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= _MAX_DOCUMENT:
            raise ScipUnavailable("invalid native SCIP definition")
        await guard()
        found = await provider.code_query(binding=binding, query=native(
            "catalog", filter={"rel_path": relative}, fields=["properties"],
        ))
        _account(cost, found)
        if found.get("receipt") != selection.receipt or found.get("error") or found.get("next_cursor"):
            raise ScipUnavailable("SCIP target receipt unavailable")
        rows = found.get("hits", [])
        if len(rows) != 1 or rows[0].get("kind") != "file":
            raise ScipUnavailable("SCIP target is outside indexed coverage")
        row = rows[0]
        row_id, props = row.get("id"), row.get("properties") or {}
        digest = props.get("source_hash")
        if (not isinstance(row_id, str) or not row_id.startswith("file:") or not _DIGEST.fullmatch(row_id[5:])
                or props.get("rel_path") != relative or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)):
            raise ScipUnavailable("SCIP target locator unavailable")
        locator = dict(file_id=row_id[5:], rel_path=relative, source_hash=digest)
        verified = await verify(client, locator, document_sha256)
        if end > len(verified):
            raise ScipUnavailable("native SCIP range exceeds admitted source")
        locator["span"] = _span(verified, start, end)
        return {"kind": "definition", "id": row_id, "source": locator}, document_sha256

    try:
        async with asyncio.timeout(12):
            await guard()
            client = _files_client(binding=binding, repository=repository, auth_manager=auth_manager,
                                   session=session, purpose=purpose)
            data = await _read(client, binding, selection.relative_path, _MAX_INDEX)
            cost["files_bytes_read"] = cost.get("files_bytes_read", 0) + len(data)
            if hashlib.sha256(data).hexdigest() != selection.digest:
                raise ScipUnavailable("selected SCIP digest changed")
            response = await _observe(provider, binding, selection.receipt, data, source, cost)
            observed = response.get("observation")
            if (not isinstance(observed, dict) or type(observed.get("incomplete")) is not bool
                    or not isinstance(observed.get("definitions"), list) or len(observed["definitions"]) > _MAX_CANDIDATES):
                raise ScipUnavailable("invalid native SCIP observation")
            original, incomplete = observed.get("source_sha256"), observed["incomplete"]
            await verify(client, source, original)
            verified_candidates = []
            for address in observed["definitions"]:
                try:
                    verified_candidates.append(await admit_target(client, address))
                except (CodeQueryError, FilesServiceError, ValueError):
                    incomplete = True
            current = await _read(client, binding, selection.relative_path, _MAX_INDEX)
            cost["files_bytes_read"] = cost.get("files_bytes_read", 0) + len(current)
            if hashlib.sha256(current).hexdigest() != selection.digest:
                raise ScipUnavailable("selected SCIP digest changed")
            await verify(client, source, original)
            # Recheck independently qualified endpoint bytes before publishing.
            candidates = []
            for candidate, digest in verified_candidates:
                await verify(client, candidate["source"], digest)
                candidates.append(candidate)
            await guard()
            if len(candidates) > 1:
                status = "ambiguous"
            elif len(candidates) == 1 and not incomplete:
                status = "observed"
            else:
                status = "unresolved"
            publish({
                "version": 1, "profile": "selected_scip_binary_v1", "status": status,
                "assertion": "index_declared", "index_sha256": selection.digest,
                "producer": response["producer"], "candidates": candidates,
                "coverage": "partial" if incomplete or not candidates else "selected_index_only",
            })
    except asyncio.CancelledError:
        raise
    except Exception:
        # The canonical syntax result remains usable when an optional source,
        # producer or authority no longer qualifies. No endpoint is retained.
        return

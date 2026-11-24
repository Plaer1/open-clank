"""S09 existing-producer read bridge; no reference writer or source crawler."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import re

from src.openclank.code_query import CodeQueryError, MAX_QUERY_WORK, query_code, validate_query

SCHEMA = "open-clank.code-resource/v1"
REQUEST_LIMIT = 16384
RESPONSE_LIMIT = 32768
STATES = {"current", "stale", "missing", "erased", "ambiguous", "unavailable", "denied", "cancelled", "invalid"}
_DOCUMENT_LOCATOR = {"type", "start", "end", "line_start", "line_end", "headings", "block_type"}
_DOCUMENT_STRUCTURE = {
    "structure_version", "start_byte", "end_byte", "section_starts", "sections",
    "omitted_sections", "table_headings", "pages", "code_mentions", "omitted_mentions",
}
_CODE_SPELLING = re.compile(r"[A-Za-z0-9._:$#/-]{1,160}")


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def validate_request(body):
    if not isinstance(body, dict) or len(_json(body)) > REQUEST_LIMIT:
        raise ValueError("Invalid resource request")
    if set(body) - {"workspace_id", "operation", "reference", "excerpt_bytes"}:
        raise ValueError("Unknown resource request field")
    workspace = body.get("workspace_id")
    operation = body.get("operation")
    if not isinstance(workspace, str) or not 1 <= len(workspace.encode()) <= 256 or operation not in ("capabilities", "resolve", "resolve_mention"):
        raise ValueError("Invalid resource selection")
    budget = body.get("excerpt_bytes", 4096)
    if type(budget) is not int or not 1 <= budget <= 8192:
        raise ValueError("Invalid excerpt budget")
    ref = body.get("reference")
    if operation == "capabilities":
        if ref is not None or "excerpt_bytes" in body:
            raise ValueError("Capabilities takes no reference")
        return workspace, operation, None, budget
    if not isinstance(ref, dict):
        raise ValueError("Typed reference required")
    kind = ref.get("kind")
    variants = {"code": {"kind", "id", "receipt"}, "memory_evidence": {"kind", "evidence_id", "source_revision"},
                "document_chunk": {"kind", "document_id", "chunk_id", "source_revision"}}
    if not isinstance(kind, str) or kind not in variants:
        raise ValueError("Invalid reference fields")
    extra = {"mention_index", "code_receipt"} if operation == "resolve_mention" else set()
    if set(ref) != variants[kind] | extra or (extra and kind != "document_chunk"):
        raise ValueError("Invalid reference fields")
    if extra:
        if type(ref["mention_index"]) is not int or not 0 <= ref["mention_index"] < 12:
            raise ValueError("Invalid mention ordinal")
        q = validate_query({"operation": "capabilities", "expected_receipt": ref["code_receipt"]})
        if q.get("expected_receipt") is None or ref["code_receipt"].get("workspace_id") != workspace:
            raise ValueError("Receipt workspace mismatch")
    if kind == "code":
        if not isinstance(ref["id"], str) or not re.fullmatch(r"(?:file|decl|occ):[0-9a-f]{64}", ref["id"]):
            raise ValueError("Invalid code identity")
        q = validate_query({"operation": "inspect", "seed_ids": [ref["id"]], "expected_receipt": ref["receipt"]})
        if q.get("expected_receipt") is None or ref["receipt"].get("workspace_id") != workspace:
            raise ValueError("Receipt workspace mismatch")
    else:
        if type(ref["source_revision"]) is not int or not 1 <= ref["source_revision"] <= 2**63 - 1:
            raise ValueError("Invalid source revision")
        for key in variants[kind] - {"kind", "source_revision"}:
            if not isinstance(ref[key], str) or not 1 <= len(ref[key].encode()) <= 256 or any(ord(c) < 32 for c in ref[key]):
                raise ValueError("Invalid record identity")
    return workspace, operation, copy.deepcopy(ref), budget


def capabilities():
    return [
        dict(kind="code", available=True, reason="Requires admitted receipt and independently current source", producer="Memery S04/S05 + Files"),
        dict(kind="memory_evidence", available=True, reason="Persisted scoped revision only; no inferred code association", producer="Existing versioned memory owner"),
        dict(kind="document_chunk", available=True, reason="Persisted scoped chunk with optional exact mention resolution", producer="Existing RAG/document and Code Graph owners"),
        *[dict(kind=k, available=False, reason="No admitted typed read producer in this bridge", producer=p) for k, p in
          [("review", "Existing review owner"), ("trace", "Existing trace/temporal owner"), ("skills", "Existing skills owner"), ("ontology", "Existing ontology/import owner")]],
        dict(kind="code_association", available=True, reason="Requires a current persisted document mention and an independently current indexed target", producer="Native document and Code Graph owners"),
    ]


def _result(workspace=None, reference=None):
    return dict(schema=SCHEMA, workspace_id=workspace, state="unavailable", reference=reference,
                validation_kind="none", current_source_verified=False, receipt=None, source=None, excerpt=None,
                provenance=None, destination=None, capabilities=capabilities(), error=None, cost={})


def _finish(result):
    for _ in range(12):
        size = len(_json(result))
        if size > RESPONSE_LIMIT:
            result.update(state="unavailable", reference=None, receipt=None, source=None, excerpt=None,
                          provenance=None, destination=None, validation_kind="none", current_source_verified=False,
                          error=dict(code="budget_exceeded", message="Resource response exceeds its bound"))
            continue
        if result["cost"].get("serialized_bytes") == size:
            return result
        result["cost"]["serialized_bytes"] = size
        result["cost"]["token_upper_bound"] = size
    raise ValueError("Cannot bound resource result")


def failure(code="invalid", *, workspace=None, cost=None):
    result = _result(workspace)
    result.update(state=code if code in STATES else "unavailable", error=dict(code=code, message="Resource evidence unavailable"))
    result["cost"] = dict(cost or {})
    return _finish(result)


def _add_cost(result, cost):
    for key, value in (cost or {}).items():
        if key not in {"serialized_bytes", "token_upper_bound", "returned"} and type(value) is int and value >= 0:
            result["cost"][key] = result["cost"].get(key, 0) + value


def _state(code):
    return "stale" if code == "stale_cursor" else code if code in STATES else "unavailable"


def _document_structure(locator, excerpt, chunk_hash):
    if not _DOCUMENT_STRUCTURE.intersection(locator):
        return
    if type(locator.get("structure_version")) is not int or locator["structure_version"] != 1 or set(locator) != _DOCUMENT_LOCATOR | _DOCUMENT_STRUCTURE:
        raise ValueError("Unknown document locator structure")
    raw = (excerpt or "").encode()
    if len(excerpt or "") != locator["end"] - locator["start"]:
        raise ValueError("Invalid canonical character range")
    from hashlib import sha256
    start, end = locator["start_byte"], locator["end_byte"]
    if (type(start) is not int or type(end) is not int or not 0 <= start <= end
            or end - start != len(raw) or sha256(raw).hexdigest() != chunk_hash):
        raise ValueError("Invalid canonical passage bytes")
    starts = locator["section_starts"]
    sections = locator["sections"]
    table = locator["table_headings"]
    pages = locator["pages"]
    mentions = locator["code_mentions"]
    if (not isinstance(starts, list) or len(starts) != len(locator["headings"])
            or any(type(value) is not int or not 0 <= value < end for value in starts)
            or not isinstance(sections, list) or len(sections) > 24
            or not isinstance(mentions, list) or len(mentions) > 12
            or (table is not None and (not isinstance(table, list) or not 2 <= len(table) <= 8
                or any(not isinstance(cell, str) or len(cell) > 32 for cell in table)))
            or (pages is not None and (not isinstance(pages, list) or len(pages) != 2
                or any(type(page) is not int or not 1 <= page <= 1024 for page in pages) or pages[0] > pages[1]))
            or any(type(locator[key]) is not int or not 0 <= locator[key] <= len(raw) for key in ("omitted_sections", "omitted_mentions"))):
        raise ValueError("Invalid document structure")
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {"start_byte", "parent_byte", "title"}
                or type(section["start_byte"]) is not int or not start <= section["start_byte"] < end
                or (section["parent_byte"] is not None and (type(section["parent_byte"]) is not int
                    or not 0 <= section["parent_byte"] < section["start_byte"]))
                or not isinstance(section["title"], str) or not 1 <= len(section["title"]) <= 48):
            raise ValueError("Invalid document section")
    for mention in mentions:
        if (not isinstance(mention, dict) or set(mention) != {"start_byte", "end_byte", "spelling"}
                or type(mention["start_byte"]) is not int or type(mention["end_byte"]) is not int
                or not start <= mention["start_byte"] < mention["end_byte"] <= end
                or not isinstance(mention["spelling"], str) or not _CODE_SPELLING.fullmatch(mention["spelling"])
                or raw[mention["start_byte"] - start:mention["end_byte"] - start] != mention["spelling"].encode()):
            raise ValueError("Invalid document code mention")


async def _code_mention(query, locator, reference):
    mentions = locator.get("code_mentions") or []
    if reference["mention_index"] >= len(mentions):
        return {"state": "unresolved", "reason": "No indexed mention at this ordinal"}
    mention = mentions[reference["mention_index"]]
    spelling = mention["spelling"]
    path, explicit, name = spelling.partition("#")
    if explicit and (not path or not name or "#" in name):
        return {"state": "unresolved", "mention": mention}
    file_only = not explicit and "/" in spelling
    if path.startswith("./"):
        path = path[2:]
    if not explicit and not file_only:
        name = spelling
        path = None
    if path is not None and (path.startswith("/") or any(part in {"", ".", ".."} for part in path.split("/"))):
        return {"state": "unresolved", "mention": mention}
    wanted = "file" if file_only else "declaration"
    fields = {"kinds": [wanted], **({"rel_path": path} if path is not None else {})}
    result = await query(dict(operation="search", query=path if file_only else name,
        expected_receipt=reference["code_receipt"], filter=fields, channels=["exact", "identifier"],
        fields=["label", "source", "properties"], budgets=dict(rows=24, bytes=16384, tokens=8192, work=MAX_QUERY_WORK)))
    if result.get("receipt") != reference["code_receipt"]:
        return {"state": "unresolved", "mention": mention, "reason": "Code receipt changed"}
    matches = []
    for hit in result.get("hits", []):
        if not isinstance(hit, dict) or hit.get("kind") != wanted:
            continue
        label = hit.get("label")
        relative = (hit.get("properties") or {}).get("rel_path")
        if not isinstance(label, str) or (path is not None and relative != path):
            continue
        if file_only or label == name or (path is None and not any(c in name for c in ".:") and re.split(r"::|\.", label)[-1] == name):
            identity = hit.get("id")
            pattern = r"file:[0-9a-f]{64}" if file_only else r"decl:[0-9a-f]{64}"
            if isinstance(identity, str) and re.fullmatch(pattern, identity):
                matches.append(identity)
    matches = sorted(set(matches))
    # A partial workspace can still prove a path-qualified occurrence;
    # unqualified discovery needs complete matching coverage.
    incomplete = bool(result.get("next_cursor") or result.get("degraded") or (path is None and result.get("partial")))
    if len(matches) > 1:
        return {"state": "ambiguous", "mention": mention, "candidates": matches}
    if not matches or incomplete:
        return {"state": "unresolved", "mention": mention, "candidate_count": len(matches), "incomplete": incomplete}
    return {"state": "candidate", "mention": mention,
            "reference": {"kind": "code", "id": matches[0], "receipt": reference["code_receipt"]}}


async def resolve_resource(*, repository, provider, auth_manager, owner, body, include_host_revision=False):
    result = _result()
    try:
        workspace, operation, reference, budget = validate_request(body)
        result = _result(workspace, reference)
        from src.openclank.workspace_policy_service import resolve_owned_workspace
        binding = resolve_owned_workspace(repository, workspace_id=workspace, owner_username=owner, auth_manager=auth_manager, purpose="app_folder")
        generation = repository.generation()
        captured = (binding.workspace, binding.path, generation)
        def check():
            now = resolve_owned_workspace(repository, workspace_id=workspace, owner_username=owner, auth_manager=auth_manager, purpose="app_folder")
            if (now.workspace, now.path, repository.generation()) != captured:
                raise CodeQueryError("denied", "Resource binding changed")
        async def query(q):
            check()
            value = await query_code(repository=repository, provider=provider, auth_manager=auth_manager, owner=owner,
                                     workspace_id=workspace, purpose="app_folder", query=q)
            _add_cost(result, value.get("cost"))
            check()
            if value.get("error"):
                raise CodeQueryError(value["error"].get("code", "unavailable"), "Resource query unavailable")
            return value
        if operation == "capabilities":
            code_capability = result["capabilities"][0]
            try:
                await query(dict(operation="capabilities", fields=[], channels=[], budgets=dict(rows=1, bytes=8192, tokens=8192, work=MAX_QUERY_WORK)))
            except CodeQueryError as exc:
                code_capability.update(available=False, reason="Current code producer unavailable: " + _state(exc.code))
            path = getattr(provider, "_fm_db_path", None)
            if not isinstance(path, str) or not Path(path).is_file():
                for cap in result["capabilities"][1:3]:
                    cap.update(available=False, reason="Existing persisted producer database unavailable")
            result.update(state="current", validation_kind="none")
        elif reference["kind"] == "code":
            expected = reference["receipt"]
            q = dict(operation="inspect", seed_ids=[reference["id"]], expected_receipt=expected,
                     fields=["source", "properties"], channels=[], budgets=dict(rows=1, bytes=16384, tokens=8192, work=MAX_QUERY_WORK))
            selected = await query(q)
            hits = selected.get("hits") or []
            if len(hits) != 1 or hits[0].get("id") != reference["id"]:
                raise CodeQueryError("missing", "Selected evidence missing")
            hit = hits[0]
            evidence_state = hit.get("evidence_state")
            if evidence_state in {"ambiguous", "stale", "unsupported"} or (hit.get("properties") or {}).get("identity_state") == "ambiguous_snapshot_local":
                raise CodeQueryError("ambiguous" if evidence_state == "ambiguous" or (hit.get("properties") or {}).get("identity_state") == "ambiguous_snapshot_local" else "stale" if evidence_state == "stale" else "unavailable", "Selected evidence not current")
            source = hit.get("source")
            if not isinstance(source, dict):
                # File catalog records have a path/hash but no parsed span.
                props = hit.get("properties") or {}
                source = dict(rel_path=props.get("rel_path"), source_hash=props.get("source_hash"), span=dict(start_byte=0))
            rel = source.get("rel_path")
            start = (source.get("span") or {}).get("start_byte", 0)
            if not isinstance(rel, str) or type(start) is not int or start < 0:
                raise CodeQueryError("unavailable", "Selected locator unavailable")
            from src.openclank.code_query import _source_guard, _files_client
            from src.openclank.files_service_client import FilesServiceError
            native = dict(owner=owner, owner_subject_id=binding.workspace.owner_subject_id, workspace_id=workspace,
                          location_id=binding.workspace.location_id, workspace_revision=binding.workspace.revision,
                          policy_generation=generation, root=binding.path, subscriber_id="query:app", origin="app")
            async def guard(fence=None):
                return await _source_guard(binding=native, repository=repository, provider=provider, auth_manager=auth_manager,
                                           session=None, purpose="app_folder", expected=expected, captured_fence=fence, cost=result["cost"])
            fence = await guard()
            client = _files_client(binding=native, repository=repository, auth_manager=auth_manager, session=None, purpose="app_folder")
            metadata_error = None
            metadata = None
            try:
                metadata = await client.request("stat", str(Path(binding.path) / rel), {"include_fingerprint": False})
                result["cost"]["files_metadata_requests_completed"] = 1
            except FilesServiceError as exc:
                metadata_error = exc.code
            await guard(fence)
            if metadata_error:
                raise CodeQueryError("missing" if metadata_error == "invalid_path" else "denied" if metadata_error == "denied" else "unavailable", "Source metadata unavailable")
            data = metadata.get("data") or {}
            canonical_path = data.get("path")
            if not isinstance(canonical_path, str) or str(data.get("kind", "")).lower() != "file":
                raise CodeQueryError("missing", "Source target is not a file")
            from src.openclank.files_host_provider import _origin
            if _origin(canonical_path) != _origin(str(Path(binding.path) / rel)):
                # A transient alias can be restored before S05's pinned read.
                # Never combine that later Current source with another path's
                # earlier metadata, even when the two targets have equal bytes.
                raise CodeQueryError("stale", "Source metadata target changed")
            read = await query(dict(operation="read", expected_receipt=expected, fields=["source", "excerpt"], channels=["raw"],
                                    read=dict(rel_path=rel, start_byte=start, length=budget), budgets=dict(rows=1, bytes=16384, tokens=8192, work=MAX_QUERY_WORK)))
            current = next((h for h in read.get("hits", []) if h.get("current_source_verified") is True), None)
            if not current or not isinstance(current.get("source"), dict):
                raise CodeQueryError("stale", "Source not independently current")
            if current["source"].get("source_hash") != source.get("source_hash"):
                raise CodeQueryError("stale", "Source hash changed")
            # Name an already validated resource through the existing issuer.
            # This is not permission and does not pin future Editor bytes.
            from src.openclank.resource_refs import issue_resource_ref
            await guard(fence)
            # S05 verified the actual relative target through the narrowed Files
            # owner; no Python resolve/stat/read or unbounded fingerprint here.
            ref = issue_resource_ref(owner_subject_id=native["owner_subject_id"], provider="host", origin_id=_origin(canonical_path),
                                     kind="file", capabilities=("stat", "open"), policy_generation=generation,
                                     location_id=native["location_id"], workspace_id=workspace)
            await guard(fence)
            check()
            destination = dict(provider="host", resource=ref.public_dict(), parent=None,
                               semantics="current resource, not retained indexed revision")
            if include_host_revision:
                raw = current.get("raw_source") or {}
                digest, size = raw.get("hash"), raw.get("file_size")
                if (raw.get("hash_scope") != "file" or raw.get("hash_algorithm") != "sha256"
                        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or type(size) is not int or size < 0):
                    raise CodeQueryError("stale", "Exact Files revision is unavailable")
                destination["expected_revision"] = {"kind":"hostFingerprint", "value":f"sha256:{digest}:{size}"}
            result.update(state="current", validation_kind="current_source", current_source_verified=True,
                          receipt=read["receipt"], source=current["source"], excerpt=current.get("excerpt"),
                          provenance=dict(producer="Memery S04/S05", hash_algorithm="blake3", hash_input="acquired_source_bytes", locator_unit="utf8_bytes_end_exclusive"),
                          destination=destination)
        else:
            from src.memory_versioned import read_source_reference
            path = getattr(provider, "_fm_db_path", None)
            if not isinstance(path, str) or not path:
                raise CodeQueryError("unavailable", "Persisted producer unavailable")
            source_workspace = workspace
            row = await asyncio.to_thread(read_source_reference, db_path=path, owner=owner, workspace_id=source_workspace, reference=reference)
            if row is None and operation == "resolve_mention":
                # Explicit owner-private personal uploads are not mirrored
                # into a Code workspace. The typed selected document may be
                # owner-wide while the candidate Code scope stays bound.
                source_workspace = ""
                row = await asyncio.to_thread(read_source_reference, db_path=path, owner=owner, workspace_id=source_workspace, reference=reference)
                if row is not None and row["source_type"] != "owner_imported_file":
                    row = None
            check()
            if row is None:
                raise CodeQueryError("missing", "Producer record missing")
            result["cost"]["persisted_records_read"] = 1
            if row["forget_state"] != "active":
                result.update(state="erased", validation_kind="persisted_revision")
            elif row.get("superseded_quote") or row["latest_revision"] != row["source_revision"]:
                result.update(state="stale", validation_kind="persisted_revision")
            else:
                locator = row.get("locator_json")
                if not locator or not row.get("source_id") or not re.fullmatch(r"[0-9a-f]{64}", row.get("source_hash") or "") or not row.get("source_type"):
                    raise CodeQueryError("unavailable", "Producer projection malformed or exceeds bound")
                if row.get("excerpt_oversize"):
                    raise CodeQueryError("unavailable", "Producer excerpt exceeds bounded projection")
                try:
                    locator = json.loads(locator)
                except (ValueError, RecursionError):
                    raise CodeQueryError("unavailable", "Producer locator malformed") from None
                kind = reference["kind"]
                if not isinstance(locator, dict):
                    raise CodeQueryError("unavailable", "Producer locator malformed")
                if kind == "memory_evidence":
                    if row["locator_type"] != "memory_record" or set(locator) != {"record_id", "source_revision"} or type(locator["source_revision"]) is not int or locator["source_revision"] != row["source_revision"] or not isinstance(locator["record_id"], str) or not re.fullmatch(r"[\w.:-]{1,256}", locator["record_id"]):
                        raise CodeQueryError("unavailable", "Memory locator producer not admitted")
                    algorithm, encoding, unit = "sha256", "canonical_json_value_utf8", "memory_record_revision"
                else:
                    if locator.get("type") != "text" or set(locator) - (_DOCUMENT_LOCATOR | _DOCUMENT_STRUCTURE):
                        raise CodeQueryError("unavailable", "Document locator producer not admitted")
                    for key in ("start", "end", "line_start", "line_end"):
                        if type(locator.get(key)) is not int or locator[key] < 0:
                            raise CodeQueryError("unavailable", "Invalid document range")
                    if locator["end"] < locator["start"] or locator["line_end"] < locator["line_start"]:
                        raise CodeQueryError("unavailable", "Invalid document range")
                    headings = locator.get("headings", [])
                    if not isinstance(headings, list) or len(headings) > 32 or any(not isinstance(h, str) or len(h.encode()) > 256 for h in headings) or not isinstance(locator.get("block_type", ""), str) or len(locator.get("block_type", "").encode()) > 128:
                        raise CodeQueryError("unavailable", "Invalid document metadata")
                    _document_structure(locator, row.get("excerpt"), row.get("chunk_hash"))
                    algorithm, encoding, unit = "sha256", "utf8_text", "unicode_characters_end_exclusive"
                result.update(state="current", validation_kind="persisted_revision",
                              excerpt=(row.get("excerpt") or "").encode()[:budget].decode("utf8", errors="ignore"),
                              provenance=dict(producer="Existing versioned memory owner" if kind == "memory_evidence" else "Existing RAG/document owner",
                                  source_id=row["source_id"], source_revision=row["source_revision"], source_hash=row["source_hash"],
                                  hash_algorithm=algorithm, hash_input=encoding, locator_unit=unit, locator=locator,
                                  code_association="unavailable: no admitted typed code locator"))
                if operation == "resolve_mention" and result["state"] == "current":
                    association = await _code_mention(query, locator, reference)
                    target_reference = association.get("reference")
                    if target_reference:
                        target = await resolve_resource(repository=repository, provider=provider, auth_manager=auth_manager, owner=owner,
                            body={"workspace_id": workspace, "operation": "resolve", "reference": target_reference, "excerpt_bytes": 256})
                        _add_cost(result, target.get("cost"))
                        if target.get("state") == "current" and target.get("current_source_verified") is True:
                            association.update(state="current", source=target["source"], destination=target["destination"])
                        else:
                            association.update(state="unresolved", reason=target.get("state", "unavailable"))
                    result["provenance"]["code_association"] = association
            # Re-read the exact bounded producer row after awaited work; no cache
            # survives forget, supersession or a deleted producer record.
            latest = await asyncio.to_thread(read_source_reference, db_path=path, owner=owner, workspace_id=source_workspace, reference=reference)
            check()
            if latest != row:
                raise CodeQueryError("missing" if latest is None else "erased" if latest.get("forget_state") != "active" else "stale", "Producer revision changed")
            result["cost"]["persisted_records_read"] += 1
        check()
        return _finish(result)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        from src.openclank.workspace_policy_service import WorkspacePolicyServiceError
        code = "denied" if isinstance(exc, WorkspacePolicyServiceError) else exc.code if isinstance(exc, CodeQueryError) else "invalid" if isinstance(exc, (ValueError, TypeError, KeyError, RecursionError)) else "unavailable"
        return failure(_state(code), workspace=result.get("workspace_id"), cost=result.get("cost"))

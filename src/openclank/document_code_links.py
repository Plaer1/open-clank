"""Qualified, read-only links between committed Copal passages and Code."""
from __future__ import annotations

import hashlib
import re

from src.openclank.code_query import CodeQueryError, MAX_QUERY_WORK, query_code, validate_query
from src.openclank.code_resource_bridge import _CODE_SPELLING, _code_mention, resolve_resource

MAX_DOCUMENTS = 16
MAX_DOCUMENT_BYTES = 64 * 1024
MAX_BATCH_BYTES = 256 * 1024
MAX_CITATIONS = 32
_CODE_ID = re.compile(r"(?:file|decl):[0-9a-f]{64}")


def document_text(document, *, limit=MAX_DOCUMENT_BYTES):
    text = document.get("text")
    if (document.get("kind") not in {"note", "wiki"} or document.get("deleted")
            or document.get("note_error") or not isinstance(text, str)
            or not 1 <= len(text.encode()) <= limit):
        return None
    return text


def scope(request, owner, body):
    state = request.app.state
    repository = getattr(state, "files_policy_repository", None)
    auth = getattr(state, "auth_manager", None)
    provider = getattr(state, "memory_provider", None)
    workspace = body.get("codeWorkspaceId")
    expected = body.get("codeReceipt")
    validated = validate_query({"operation":"capabilities", "expected_receipt":expected})
    if (repository is None or auth is None or provider is None or not isinstance(workspace, str)
            or validated.get("expected_receipt") is None or expected["workspace_id"] != workspace):
        raise ValueError("Code workspace is unavailable")
    account = auth.account_id(owner)
    def current():
        return bool(account and auth.account_id(owner) == account and auth.username_for_account_id(account) == owner
                    and getattr(state, "files_policy_repository", None) is repository
                    and getattr(state, "auth_manager", None) is auth)
    if not current():
        raise ValueError("Code workspace is unavailable")

    async def query(body):
        if not current():
            raise ValueError("Code workspace changed")
        result = await query_code(repository=repository, provider=provider, auth_manager=auth, owner=owner,
                                  workspace_id=workspace, purpose="app_folder", query=body)
        if not current() or result.get("error") or result.get("receipt") != expected:
            raise ValueError("Code workspace changed or query unavailable")
        return result

    async def target(reference):
        value = await resolve_resource(repository=repository, provider=provider, auth_manager=auth, owner=owner,
            body={"workspace_id":workspace, "operation":"resolve", "reference":reference, "excerpt_bytes":256},
            include_host_revision=True)
        if not current() or value.get("state") != "current" or value.get("current_source_verified") is not True:
            raise ValueError("Code source is unavailable or changed")
        return value

    async def segments(texts):
        return await _segments(request, owner, texts, current)
    return expected, query, target, segments, current


async def _segments(request, owner, texts, current):
    state = request.app.state
    provider = getattr(state, "canonical_memory_provider", None) or getattr(state, "memory_provider", None)
    project = getattr(provider, "_host_document_segments", None)
    if not callable(project):
        raise ValueError("Native document structure is unavailable")
    from src.memory_provider import MemoryRequestRejectedError
    try:
        return await project(owner=owner, texts=texts, current=lambda: current()
                             and (getattr(state, "canonical_memory_provider", None) or getattr(state, "memory_provider", None)) is provider)
    except MemoryRequestRejectedError as exc:
        raise ValueError("Native document structure is unavailable") from exc


def _checked_segments(text, result):
    raw = text.encode()
    if (not isinstance(result, dict) or set(result) != {"mentions", "omitted"}
            or not isinstance(result["mentions"], list) or len(result["mentions"]) > 64
            or type(result["omitted"]) is not int or not 0 <= result["omitted"] <= len(raw)):
        raise ValueError("Invalid native mention projection")
    prior = 0
    for item in result["mentions"]:
        if (not isinstance(item, dict) or set(item) != {"start_byte", "end_byte", "line", "spelling"}
                or type(item["start_byte"]) is not int or type(item["end_byte"]) is not int
                or not prior <= item["start_byte"] < item["end_byte"] <= len(raw)
                or type(item["line"]) is not int or not 1 <= item["line"] <= len(raw)
                or not isinstance(item["spelling"], str) or not _CODE_SPELLING.fullmatch(item["spelling"])
                or raw[item["start_byte"]:item["end_byte"]] != item["spelling"].encode()
                or raw.count(b"\n", 0, item["start_byte"]) + 1 != item["line"]):
            raise ValueError("Invalid native mention witness")
        prior = item["end_byte"]
    return result


def _line_anchor(raw, start, end):
    before = raw.rfind(b"\n", 0, start) + 1
    after = raw.find(b"\n", end)
    if after < 0:
        after = len(raw)
    return hashlib.sha256(b"openclank.copal-citation-line.v1\0" + raw[before:after].strip(b" \t\r")).hexdigest()


def _passage(document, raw, mention):
    start, end = mention["start_byte"], mention["end_byte"]
    return {"documentId":document["id"], "revisionId":str(document["head"]), "name":str(document.get("name") or "Document")[:256],
            "spelling":mention["spelling"], "line":mention["line"], "startByte":start, "endByte":end,
            "anchorProof":_line_anchor(raw, start, end)}


async def relocate(request, owner, document, body, *, current):
    if (set(body) != {"spelling", "anchorProof"} or not isinstance(body["spelling"], str)
            or not _CODE_SPELLING.fullmatch(body["spelling"]) or not isinstance(body["anchorProof"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", body["anchorProof"])):
        raise ValueError("Invalid citation anchor")
    text = document_text(document)
    if text is None or not document.get("head") or not current():
        raise ValueError("Document source unavailable")
    result = _checked_segments(text, (await _segments(request, owner, [text], current))[0])
    raw = text.encode()
    candidates = [_passage(document, raw, item) for item in result["mentions"]
                  if item["spelling"] == body["spelling"]
                  and _line_anchor(raw, item["start_byte"], item["end_byte"]) == body["anchorProof"]]
    # A truncated parse cannot prove that the witnessed line moved uniquely.
    if not current() or result["omitted"]:
        return {"state":"unresolved", "candidates":[]}
    state = "relocated" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved"
    return {"state":state, "candidates":candidates[:16], "omittedCandidates":max(0,len(candidates)-16)}


async def forward(request, owner, body, document, *, copal_scope=None):
    if set(body) != {"codeWorkspaceId", "codeReceipt", "revisionId", "spelling", "startLine", "endLine"}:
        raise ValueError("Invalid code reference")
    spelling, begin, end = body["spelling"], body["startLine"], body["endLine"]
    if (not isinstance(spelling, str) or not _CODE_SPELLING.fullmatch(spelling)
            or type(begin) is not int or type(end) is not int or not 1 <= begin <= end
            or not isinstance(body["revisionId"], str) or str(document.get("head")) != body["revisionId"]):
        raise ValueError("The document revision changed")
    text = document_text(document)
    if text is None:
        raise ValueError("Document source unavailable")
    receipt, query, target, segments, current = scope(request, owner, body)
    retained = None
    if copal_scope is not None:
        from src.openclank.document_ingestion import read_journal
        retained = await read_journal(request, copal_scope, document, audience_current=current)
    parsed = _checked_segments(text, retained if retained is not None else (await segments([text]))[0])
    matches = [mention for mention in parsed["mentions"] if mention["spelling"] == spelling and begin <= mention["line"] <= end]
    if len(matches) != 1:
        return {"state":"ambiguous" if len(matches) > 1 else "unresolved", "omittedMentions":parsed["omitted"]}
    result = await _code_mention(query, {"code_mentions": matches}, {"mention_index":0, "code_receipt":receipt})
    if result["state"] == "candidate":
        try:
            value = await target(result["reference"])
        except (ValueError, CodeQueryError):
            return {"state":"unresolved", "mention":matches[0]}
        result.update(state="current", source=value["source"], destination=value["destination"])
    return result


async def reverse(request, owner, body, documents, *, current_document, module_outline=None):
    if set(body) != {"codeWorkspaceId", "codeReceipt", "resourceId"}:
        raise ValueError("Invalid code selection")
    resource = body["resourceId"]
    if not isinstance(resource, str) or not _CODE_ID.fullmatch(resource):
        raise ValueError("Invalid code selection")
    receipt, query, target, segments, current = scope(request, owner, body)
    reference = {"kind":"code", "id":resource, "receipt":receipt}
    value = await target(reference)
    selected = await query(dict(operation="inspect", seed_ids=[resource], expected_receipt=receipt,
        fields=["label","source","properties"], channels=[], budgets=dict(rows=1, bytes=16384, tokens=8192, work=MAX_QUERY_WORK)))
    hits = selected.get("hits") or []
    node = hits[0] if len(hits) == 1 else {}
    label = node.get("label")
    path = (value.get("source") or {}).get("rel_path")
    if node.get("id") != resource or not isinstance(label, str) or not isinstance(path, str):
        raise ValueError("Code source changed")

    # An exact relative file or path plus fully qualified name is evidence.
    # A bare equal spelling in a different scope cannot establish a backlink.
    wanted = {f"./{path}"} if resource.startswith("file:") else {f"{path}#{label}", f"./{path}#{label}"}
    if resource.startswith("file:") and "/" in path:
        wanted.add(path)
    if (module_outline is not None and resource.startswith("file:") and module_outline.get("receipt") == receipt
            and not module_outline.get("next_cursor") and not module_outline.get("degraded")):
        names = {}
        for hit in module_outline.get("hits", []):
            if hit.get("kind") != "declaration" or (hit.get("properties") or {}).get("rel_path") != path:
                continue
            name = hit.get("label")
            if isinstance(name, str) and _CODE_SPELLING.fullmatch(name):
                names.setdefault(name, []).append(hit)
        for name, matching in names.items():
            if len(matching) == 1 and (matching[0].get("properties") or {}).get("identity_state") != "ambiguous_snapshot_local":
                wanted.update({f"{path}#{name}", f"./{path}#{name}"})
    chosen = []
    total_bytes = 0
    omitted_documents = 0
    for document in sorted(documents, key=lambda row: str(row.get("id"))):
        if document.get("kind") not in {"note", "wiki"}:
            continue
        text = document_text(document)
        if text is None or len(chosen) >= MAX_DOCUMENTS or total_bytes + len(text.encode()) > MAX_BATCH_BYTES:
            omitted_documents += 1
            continue
        total_bytes += len(text.encode())
        chosen.append((document, text))
    projected = await segments([text for _, text in chosen]) if chosen else []
    citations = []
    omitted_mentions = 0
    for ((document, text), parsed) in zip(chosen, projected, strict=True):
        validated = _checked_segments(text, parsed)
        omitted_mentions += validated["omitted"]
        raw = text.encode()
        for mention in validated["mentions"]:
            if mention["spelling"] not in wanted:
                continue
            if len(citations) == MAX_CITATIONS:
                omitted_mentions += 1
                continue
            if document.get("head"):
                citations.append(_passage(document, raw, mention))
    chosen_by_id = {document["id"]:document for document, _ in chosen}
    for ident in dict.fromkeys(item["documentId"] for item in citations):
        latest = await current_document(ident)
        recorded = chosen_by_id[ident]
        if latest.get("head") != recorded.get("head") or latest.get("text") != recorded.get("text"):
            raise ValueError("A citing document changed")
    await target(reference)
    if not current():
        raise ValueError("Code or document access changed")
    coverage = getattr(documents, "coverage", {})
    return {"citations":citations, "coverage":{"complete":coverage.get("complete") is True
        and not omitted_documents and not omitted_mentions, "omittedDocuments":omitted_documents,
        "omittedMentions":omitted_mentions, "scope":"explicit_relative_path_mentions"}}

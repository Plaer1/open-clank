"""Default-off, nonsemantic current-source cards over the C04 accessor.

No session-history cache: dedup examines only the actual retained payload
passed by its owner, so compaction/reset releases suppression naturally.
"""
from __future__ import annotations
import json
import re

from src.openclank.code_query import MAX_QUERY_WORK, query_code

SENTINEL = "[code-evidence-v1 "


def code_signal(message):
    if not isinstance(message, str) or len(message.encode()) > 65536:
        return None
    for value in re.findall(r"`([^`\n]{1,128})`", message):
        if re.fullmatch(r"[\w./:-]+", value, re.UNICODE):
            return value
    match = re.search(r"\b(?:function|method|class|symbol|callers?\s+of|callees?\s+of)\s+([\w.:]+)", message, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?<!\w)([\w./-]+\.(?:py|rs|ts|tsx|js|jsx))\b", message)
    if match:
        return match.group(1)
    if re.search(r"\b(?:code|source|implement|refactor|debug|fix|function|symbol)\b", message, re.I):
        for value in re.findall(r"\b\w+\b", message, re.UNICODE):
            if "_" in value or re.search(r"[a-z][A-Z]", value):
                return value
    return None


def _payload_size(messages):
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())


def _text_cost_known(messages):
    """Text/tool JSON bytes are bounded; a short media URL is not a token bound."""
    def content(value, depth=0):
        if depth > 32:
            return False
        if value is None or isinstance(value, str):
            return True
        if not isinstance(value, list):
            return False
        for part in value:
            if not isinstance(part, dict):
                return False
            kind = part.get("type")
            if kind in {"text", "reasoning"} and isinstance(part.get("text"), str):
                continue
            if kind in {"tool-call", "tool_call", "tool_use"}:
                continue  # Arguments are serialized JSON, not rendered media.
            if kind in {"tool-result", "tool_result"}:
                output = part.get("output")
                if isinstance(output, dict) and output.get("type") in {"text", "json", "error-text", "error-json"}:
                    continue  # Preserve ordinary file/image metadata as JSON.
                if content(part.get("content"), depth + 1) and "content" in part:
                    continue
            return False
        return True
    try:
        _payload_size(messages)  # Reject non-JSON bytes/objects/NaN metadata.
        return all(isinstance(m, dict) and content(m.get("content")) for m in messages)
    except (TypeError, ValueError, RecursionError):
        return False


def _retained_text(messages):
    # Includes tool output/structured content exactly in this retained payload,
    # not the broader archival Session.history projection.
    strings = []
    def visit(value, depth=0):
        if depth > 32:
            return
        if isinstance(value, str):
            strings.append(value)
            # A tool's JSON text is retained as a string. Decode its structure
            # for comparison instead of comparing double-escaped excerpts.
            if value.startswith(("{", "[")):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    return
                if not isinstance(parsed, str):
                    visit(parsed, depth + 1)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
    visit(messages)
    return "\n".join(strings)


def _retained_window(messages, *, receipt, source, text):
    """A retained typed manual result may intentionally omit BLAKE3 locators.
    Compare its canonical ID/current receipt and byte window; SHA256 observation
    hashes are never compared to S04 BLAKE3 hashes or used as index authority.
    """
    if not isinstance(receipt, dict) or not source.get("file_id"):
        return False
    span = source.get("span") or {}
    def visit(value, depth=0):
        if depth > 32:
            return False
        if isinstance(value, str) and len(value.encode()) <= 262144 and value.startswith(("{", "[")):
            try:
                return visit(json.loads(value), depth + 1)
            except (ValueError, RecursionError):
                return False
        if isinstance(value, list):
            return any(visit(child, depth + 1) for child in value)
        if not isinstance(value, dict):
            return False
        if value.get("schema") == "open-clank.code-query/v1" and value.get("receipt") == receipt and not value.get("error"):
            for hit in value.get("hits", []):
                if not isinstance(hit, dict) or hit.get("id") != "file:" + source["file_id"] or hit.get("current_source_verified") is not True:
                    continue
                raw, excerpt = hit.get("raw_source") or {}, hit.get("excerpt")
                locator = hit.get("source") or {}
                if (locator.get("file_id") == source["file_id"] and locator.get("rel_path") == source.get("rel_path")
                        and locator.get("source_hash") == source.get("source_hash") and isinstance(excerpt, str)):
                    located = locator.get("span") or {}
                    a, b, start, end = located.get("start_byte"), located.get("end_byte"), span.get("start_byte"), span.get("end_byte")
                    if all(type(n) is int for n in (a, b, start, end)) and 0 <= a <= start <= end <= b and len(excerpt.encode()) == b-a and excerpt.encode()[start-a:end-a] == text.encode():
                        return True
                if raw.get("rel_path") != source.get("rel_path") or raw.get("hash_algorithm") != "sha256" or raw.get("hash_scope") not in {"file", "byte_range"} or not isinstance(excerpt, str):
                    continue
                a, b, start, end = raw.get("start_byte"), raw.get("end_byte"), span.get("start_byte"), span.get("end_byte")
                if all(type(n) is int for n in (a, b, start, end)) and 0 <= a <= start <= end <= b and len(excerpt.encode()) == b - a and excerpt.encode()[start-a:end-a] == text.encode():
                    return True
        return any(visit(child, depth + 1) for child in value.values())
    return any(visit(message.get("content")) for message in messages if message.get("role") == "tool")


async def append_code_context(*, messages, message, session, provider, repository, auth_manager, prefs, owner,
        use_memory=True, incognito=False, max_bytes=2048, max_tokens=2048, context_length=None, prefs_reader=None):
    initial_workspace = getattr(session, "workspace_id", None)
    def eligible():
        current = prefs_reader() if callable(prefs_reader) else prefs
        return (isinstance(current, dict) and current.get("code_context_enabled") is True and current.get("memory_enabled") is not False and current.get("memory_mode") != "off"
            and session is not None and getattr(session, "owner", None) == owner and getattr(session, "workspace_id", None) == initial_workspace
            and not getattr(session, "incognito", False) and not getattr(session, "archived", False))
    if (not isinstance(messages, list) or not _text_cost_known(messages) or not owner or not use_memory or incognito or session is None
            or getattr(session, "incognito", False) or getattr(session, "archived", False)
            or getattr(session, "owner", None) != owner or not getattr(session, "workspace_id", None)
            or not isinstance(prefs, dict) or prefs.get("code_context_enabled") is not True
            or prefs.get("memory_enabled") is False or prefs.get("memory_mode") == "off"):
        return messages
    signal = code_signal(message)
    if not signal or type(max_bytes) is not int or type(max_tokens) is not int:
        return messages
    budget = min(2048, max_bytes, max_tokens)
    if context_length is not None:
        if type(context_length) is not int or context_length <= 0:
            return messages
        # Byte-per-token upper bound includes the complete retained envelope;
        # conservative under-injection is preferable to context overflow.
        budget = min(budget, context_length - _payload_size(messages) - 1024)
    if budget < 768:
        return messages
    try:
        result = await query_code(repository=repository, provider=provider, auth_manager=auth_manager, owner=owner,
            workspace_id=session.workspace_id, purpose="agent_workspace", session=session,
            query=dict(operation="search", query=signal, channels=["exact", "identifier"], fields=["label", "source", "properties"], budgets=dict(rows=3, bytes=8192, tokens=8192, hops=0, work=MAX_QUERY_WORK)))
        if result.get("error"):
            return messages
        if not eligible():
            return messages
        for hit in result.get("hits", []):
            props = hit.get("properties") or {}
            if (hit.get("kind") not in {"declaration", "file"} or hit.get("evidence_state") in {"stale", "suggestion", "ambiguous", "unsupported"}
                    or props.get("identity_state", "stable") != "stable"
                    or not any(m.get("channel") in {"exact", "identifier"} for m in hit.get("matches", []))):
                continue
            source = hit.get("source") or {}
            path = source.get("rel_path") or props.get("rel_path")
            start = (source.get("span") or {}).get("start_byte", 0)
            if not isinstance(path, str) or type(start) is not int:
                continue
            window = await query_code(repository=repository, provider=provider, auth_manager=auth_manager, owner=owner,
                workspace_id=session.workspace_id, purpose="agent_workspace", session=session,
                query=dict(operation="read", read=dict(rel_path=path, start_byte=start, length=min(1024, budget - 600)),
                    expected_receipt=result.get("receipt"), fields=["source", "excerpt", "properties"], channels=["raw"],
                    budgets=dict(rows=1, bytes=8192, tokens=8192, hops=0, work=MAX_QUERY_WORK)))
            if window.get("error"):
                continue
            if not eligible():
                return messages
            for observed in window.get("hits", []):
                src, text = observed.get("source"), observed.get("excerpt")
                if observed.get("current_source_verified") is not True or not isinstance(src, dict) or not isinstance(text, str) or not text:
                    continue
                span = src.get("span") or {}
                key = f"{session.workspace_id} {src.get('file_id')} {src.get('source_hash')} {span.get('start_byte')}:{span.get('end_byte')}"
                retained = _retained_text(messages)
                if SENTINEL + key in retained or _retained_window(messages, receipt=window.get("receipt"), source=src, text=text):
                    return messages
                from src.prompt_security import untrusted_context_message
                card = untrusted_context_message("verified code source", f"{SENTINEL}{key}]\nObserved source, not runtime behavior. {src['rel_path']}:{span.get('start_line')}-{span.get('end_line')}\n{text}")
                size = _payload_size(card)
                if size > budget:
                    continue
                out = list(messages)
                at = next((i for i in range(len(out) - 1, -1, -1) if out[i].get("role") == "user"), len(out))
                out.insert(at, card)
                if context_length is not None and _payload_size(out) + 1024 > context_length:
                    return messages
                if not eligible():
                    return messages
                return out
    except Exception:
        return messages
    return messages

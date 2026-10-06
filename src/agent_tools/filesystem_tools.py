import asyncio
import codecs
from contextlib import asynccontextmanager
from dataclasses import dataclass
import functools
import json
import os
import re
import difflib
import fnmatch
import hashlib
import shutil
import pathlib
import threading
import time
import uuid
import weakref
from typing import Optional, Dict, Any, Tuple, List, Awaitable, Callable

from src.constants import MAX_READ_CHARS, MAX_DIFF_LINES, MAX_OUTPUT_CHARS
from core.atomic_io import (
    AtomicFileChange,
    AtomicWriteConflict,
    atomic_write_batch,
    atomic_write_bytes,
    file_fingerprint,
    fingerprint_bytes,
)
from src.openclank.history_capture import HistoryContext, context_from_mapping, last_history_status


def _history_context(ctx: dict):
    """Require explicit authenticated capture intent for Python file writes."""
    trusted = ctx.get("history_context") if isinstance(ctx, dict) else None
    if isinstance(trusted, HistoryContext):
        return trusted
    return context_from_mapping(ctx)

_CODENAV_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "__pycache__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".cache", "site-packages", ".idea", ".tox",
})
_CODENAV_MAX_HITS = 200
_CODENAV_MAX_LINE = 400
_FILE_RESULT_CONTRACT = "open-clank.file-result/v1"


def _agent_service_enabled(ctx: dict) -> bool:
    """Canonical Rust enforcement is mandatory, including an empty scope."""
    return True


async def _agent_request(ctx: dict, tool: str, operation: str, path: str, payload: Optional[dict] = None) -> Optional[dict]:
    if not _agent_service_enabled(ctx):
        return None
    from src.openclank.files_service_client import FilesServiceError, agent_client_for
    from src.tool_execution import get_active_workspace
    owner = str(ctx.get("owner") or "").strip()
    if not owner:
        return {"error": f"{tool}: authenticated owner is required", "exit_code": 1, "blocked": True, "code": "denied"}
    try:
        response = await agent_client_for(owner, get_active_workspace(), workspace_id=str(ctx.get("authority_workspace_id") or ""),
            chat_id=str(ctx.get("session_id") or "")).request(operation, path, payload)
    except FilesServiceError as error:
        return {
            "error": f"{tool}: {error}",
            "exit_code": 1,
            "blocked": error.code in {"denied", "outside_root", "capability_denied"},
            "code": error.code,
        }
    return response


def _agent_data(response: dict) -> dict:
    return response.get("data") if isinstance(response.get("data"), dict) else {}


def _agent_fingerprint(data: dict) -> Optional[str]:
    value = data.get("fingerprint")
    if value is None:
        for key in ("Created", "Replaced", "created", "replaced"):
            nested = data.get(key)
            if isinstance(nested, dict):
                value = nested.get("fingerprint")
                break
    if isinstance(value, dict):
        return str(value.get("value") or "") or None
    return str(value or "") or None


async def _agent_mutation_fingerprint(
    ctx: dict,
    path: str,
) -> tuple[Optional[dict], Optional[str]]:
    """Read the exact Rust-authorized identity before interactive mutation.

    The returned fingerprint is repeated in the mutation request, so the Rust
    engine rejects a source changed while the approval prompt was open.
    """
    response = await _agent_request(
        ctx,
        "manage_files",
        "stat",
        path,
        {"include_fingerprint": True},
    )
    if response is None or response.get("code"):
        return response, None
    fingerprint = _agent_fingerprint(_agent_data(response))
    if not fingerprint:
        return ({
            "error": "manage_files: the source has no stable regular-file fingerprint",
            "exit_code": 1,
            "code": "invalid_path",
        }, None)
    return None, fingerprint


def _agent_encoding(value: Any) -> str:
    return {
        "Utf8": "utf-8", "Utf16Le": "utf-16-le", "Utf16Be": "utf-16-be",
        "Utf32Le": "utf-32-le", "Utf32Be": "utf-32-be",
    }.get(str(value), str(value or "utf-8").lower().replace("utf8", "utf-8"))


def _agent_newline(value: Any) -> str:
    return {"CRLF": "crlf", "LF": "lf", "CR": "cr"}.get(str(value), str(value or "lf").lower())


def _agent_target_path(raw_path: str, *, directory_default: bool = False) -> str:
    """Resolve syntax for the Rust lane without creating a second authority.

    Legacy ``_resolve_tool_path`` also applies the historical tmp/extra-roots
    allowlist. Running a Rust-scoped request through it made Settings grants
    unusable outside that unrelated list. Here we only make relative input
    deterministic; ``odysseus-files`` performs the canonical containment and
    capability decision for every operation.
    """
    from src.tool_execution import get_active_workspace

    value = os.path.expanduser(str(raw_path or "").strip())
    workspace = str(get_active_workspace() or "").strip()
    if not value:
        if directory_default and workspace:
            value = workspace
        else:
            raise ValueError("an absolute path or active workspace is required")
    if not os.path.isabs(value):
        if not workspace:
            raise ValueError("relative paths require an active workspace")
        value = os.path.join(workspace, value)
    return os.path.abspath(os.path.normpath(value))


async def _agent_read_file(content: str, ctx: dict) -> Optional[dict]:
    args = _tool_args(content)
    raw_path = str(args.get("path", "")).strip() if args else (content or "").split("\n", 1)[0].strip()
    offset = int(args.get("offset") or 0) if args else 0
    limit = int(args.get("limit") or 0) if args else 0
    cursor = max(0, int(args.get("cursor") or 0)) if args else 0
    try:
        path = _agent_target_path(raw_path)
    except ValueError as error:
        return {"error": f"read_file: {error}", "exit_code": 1}
    response = await _agent_request(ctx, "read_file", "read_lines", path, {})
    if response is None or response.get("exit_code") == 1:
        return response
    data = _agent_data(response)
    text = str(data.get("text") or "")
    fingerprint = _agent_fingerprint(data)
    encoding = _agent_encoding(data.get("encoding"))
    newline = _agent_newline(data.get("newline"))
    lines = text.splitlines(keepends=True)
    if offset > 0 or limit > 0:
        start = max(offset, 1)
        selected = lines[start - 1: start - 1 + (limit or len(lines))]
        output = "".join(selected)
        next_offset = start + len(selected) if start + len(selected) <= len(lines) else None
        page_unit, page_cursor, next_cursor = "line", start, next_offset
        total = len(lines)
    else:
        output = text[cursor: cursor + MAX_READ_CHARS]
        next_cursor = cursor + len(output) if cursor + len(output) < len(text) else None
        next_offset, page_unit, page_cursor, total = None, "character", cursor, len(text)
    result = {
        "output": output,
        "exit_code": 0,
        "path": path,
        "fingerprint": fingerprint,
        "encoding": encoding,
        "media_type": "text/plain",
        "newline": newline,
        "page": {"offset": page_cursor, "limit": limit or None, "next_offset": next_offset, "cursor": cursor, "next_cursor": next_cursor, "has_more": next_cursor is not None, "total_lines": len(lines)},
    }
    return _with_file_result(result, operation="read", path=path, kind="text", range={"unit": page_unit, "start": page_cursor, "end": page_cursor + max(0, len(output) - 1)}, page={"unit": page_unit, "cursor": page_cursor, "next_cursor": next_cursor, "has_more": next_cursor is not None, "returned": len(output), "total": total}, bytes_considered=int(data.get("size") or len(text.encode(encoding, errors="ignore"))), lines_considered=len(output.splitlines()), encoding=encoding, newline=newline, media_type="text/plain", fingerprint=fingerprint)


async def _agent_write_file(content: str, ctx: dict) -> Optional[dict]:
    args = _tool_args(content)
    if args:
        raw_path, body = str(args.get("path", "")).strip(), str(args.get("content", ""))
        expected = str(args.get("expected_fingerprint")) if args.get("expected_fingerprint") is not None else None
    else:
        lines = content.split("\n", 1)
        raw_path, body, expected = lines[0].strip(), lines[1] if len(lines) > 1 else "", None
    try:
        path = _agent_target_path(raw_path)
    except ValueError as error:
        return {"error": f"write_file: {error}", "exit_code": 1}
    snapshot = await _agent_request(ctx, "write_file", "read_lines", path, {})
    if snapshot is not None and snapshot.get("code"):
        # A missing final file is represented as ``invalid_path`` by the Rust
        # adapter and is the only read error that may flow into creation.
        # Transport/registry failures must remain fail-closed.
        if snapshot.get("code") != "invalid_path":
            return snapshot
    data = _agent_data(snapshot or {})
    exists = bool(snapshot and not snapshot.get("code"))
    if exists:
        old = str(data.get("text") or "")
        current = _agent_fingerprint(data)
        if expected is not None and expected != current:
            return {"error": f"write_file: conflict: {path} changed since the supplied fingerprint", "exit_code": 1, "conflict": True}
        newline = _agent_newline(data.get("newline"))
        rendered = _preserve_newline_style(body, {"lf": "\n", "crlf": "\r\n", "cr": "\r"}.get(newline, "\n"))
        payload = {"text": rendered, "expected_fingerprint": {"algorithm": "sha256", "value": current}} if current else {"text": rendered}
    else:
        old, rendered, payload = "", body, {"text": body}
    response = await _agent_request(ctx, "write_file", "replace", path, payload)
    if response is None or response.get("code"):
        return response
    result_data = _agent_data(response)
    fingerprint = _agent_fingerprint(result_data)
    result = {"output": f"Wrote {len(rendered.encode('utf-8'))} bytes to {path}", "exit_code": 0, "path": path, "old_fingerprint": _agent_fingerprint(data) if exists else None, "fingerprint": fingerprint}
    diff = _unified_diff(old, rendered, path)
    if diff:
        result["diff"] = diff
    return _with_file_result(result, operation="write", path=path, kind="text", range={"unit": "byte", "start": 0, "end": max(0, len(rendered.encode('utf-8')) - 1)}, page={"unit": "byte", "cursor": 0, "next_cursor": None, "has_more": False, "returned": len(rendered.encode('utf-8')), "total": len(rendered.encode('utf-8'))}, bytes_considered=len(rendered.encode('utf-8')), lines_considered=len(rendered.splitlines()), encoding=_agent_encoding(data.get("encoding")) if exists else "utf-8", newline=_agent_newline(data.get("newline")) if exists else "lf", media_type="text/plain", fingerprint=fingerprint)


async def _agent_edit_file(content: str, ctx: dict) -> Optional[dict]:
    args = _tool_args(content)
    if not args:
        return {"error": "edit_file: JSON arguments are required when the Rust agent lane is active", "exit_code": 1}
    raw_path = str(args.get("path") or "").strip()
    try:
        path = _agent_target_path(raw_path)
    except ValueError as error:
        return {"error": f"edit_file: {error}", "exit_code": 1}
    payload = {"old": str(args.get("old_string") or ""), "new": str(args.get("new_string") or ""), "replace_all": bool(args.get("replace_all"))}
    if args.get("expected_fingerprint") is not None:
        payload["expected_fingerprint"] = {"algorithm": "sha256", "value": str(args["expected_fingerprint"])}
    response = await _agent_request(ctx, "edit_file", "patch", path, payload)
    if response is None or response.get("code"):
        return response
    data = _agent_data(response)
    result = {"output": f"Edited {path} ({int(data.get('replacements') or 1)} replacement{'s' if int(data.get('replacements') or 1) != 1 else ''})", "exit_code": 0, "path": path, "old_fingerprint": (data.get("old_fingerprint") or {}).get("value"), "fingerprint": (data.get("new_fingerprint") or {}).get("value")}
    return result


async def _agent_ls(content: str, ctx: dict) -> Optional[dict]:
    args = _tool_args(content)
    raw_path = str(args.get("path", "")).strip() if args else (content or "").split("\n", 1)[0].strip()
    try:
        root = _agent_target_path(raw_path, directory_default=True)
    except ValueError as error:
        return {"error": f"ls: {error}", "exit_code": 1}
    payload = {}
    if args and isinstance(args.get("cursor"), dict):
        payload["cursor"] = args["cursor"]
    response = await _agent_request(ctx, "ls", "list_directory", root, payload)
    if response is None or response.get("code"):
        return response
    data = _agent_data(response)
    rows = []
    for entry in data.get("entries") or []:
        kind = str(entry.get("kind") or "File")
        is_dir = kind.lower() == "directory"
        name = str(entry.get("name") or "")
        rows.append((is_dir, name, int(entry.get("size") or 0)))
    lines = [f"{root}:"] + [f"  {name}/" if is_dir else f"  {name}  ({size} B)" for is_dir, name, size in rows]
    if not rows:
        lines.append("  (empty)")
    entries = [{"name": name, "path": os.path.join(root, name), "kind": "directory" if is_dir else "file", "type": "directory" if is_dir else "file", "size": size} for is_dir, name, size in rows]
    next_cursor = data.get("next_cursor")
    result = {"output": "\n".join(lines), "exit_code": 0, "path": root, "entries": entries, "page": {"cursor": payload.get("cursor"), "next_cursor": next_cursor, "has_more": next_cursor is not None, "total": None}}
    return _with_file_result(result, operation="list", path=root, kind="directory", page={"unit": "entry", "cursor": payload.get("cursor"), "next_cursor": next_cursor, "has_more": next_cursor is not None, "returned": len(entries), "total": None}, media_type="inode/directory", items=entries)


async def _agent_search(content: str, ctx: dict, *, content_search: bool) -> Optional[dict]:
    if not _agent_service_enabled(ctx):
        return None
    args = _tool_args(content)
    pattern = str(args.get("pattern") or "").strip() if args else str(content or "").strip()
    if not pattern:
        return {"error": f"{'grep' if content_search else 'glob'}: pattern is required", "exit_code": 1}
    try:
        root = _agent_target_path(
            str(args.get("path", "")) if args else "",
            directory_default=True,
        )
    except ValueError as error:
        return {"error": f"{'grep' if content_search else 'glob'}: {error}", "exit_code": 1}
    if content_search and args and not bool(args.get("literal")) and str(args.get("mode") or "").lower() not in {"literal", "fixed"}:
        return {"error": "grep: regex search is not yet exposed by the Rust service lane; use literal=true", "exit_code": 1, "code": "unsupported"}
    payload = {"query": pattern.replace("*", ""), "max_results": int(args.get("limit") or args.get("max_results") or _CODENAV_MAX_HITS) if args else _CODENAV_MAX_HITS, "max_entries": 10000, "max_depth": 32, "max_bytes_per_file": 1024 * 1024, "include_hidden": bool(args.get("include_hidden")) if args else False, "case_sensitive": not bool(args.get("ignore_case")) if args else False}
    response = await _agent_request(ctx, "grep" if content_search else "glob", "content_search" if content_search else "filename_search", root, payload)
    if response is None or response.get("code"):
        return response
    data = _agent_data(response)
    matches = [str(item) for item in (data.get("matches") or [])]
    label = "grep" if content_search else "glob"
    output = "\n".join(matches) or f"No matches for {pattern!r} under {root}"
    items = [{"path": item, "kind": "match"} for item in matches]
    result = {"output": output, "exit_code": 0, "path": root, "matches": items if content_search else None, "paths": matches if not content_search else None, "search_mode": "literal" if content_search else "filename", "page": {"cursor": int(args.get("cursor") or 0) if args else 0, "next_cursor": None, "has_more": not bool(data.get("complete", True)), "result_count": len(matches)}}
    return _with_file_result(result, operation=label, path=root, kind="search", page={"unit": "result", "cursor": 0, "next_cursor": None, "has_more": not bool(data.get("complete", True)), "returned": len(matches), "total": len(matches)}, search_mode="literal" if content_search else "filename", items=items)


class ProjectPolicyBlocked(PermissionError):
    def __init__(self, message: str, *, findings: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.findings = findings or []


def _validate_file_policy(
    ctx: dict,
    candidates: Dict[str, Optional[bytes]],
) -> Dict[str, Any]:
    """Run exact native-file candidates through the one project-policy gate."""
    owner = str(ctx.get("owner") or "").strip()
    workspace = str(ctx.get("workspace") or "").strip()
    if not owner or not workspace or not candidates:
        return {"enforced": False, "allowed": True, "findings": [], "warnings": []}
    from src.constants import FM_DB_PATH
    from src.project_hex import HexResolutionError, validate_registered_project_candidates

    try:
        result = validate_registered_project_candidates(
            owner=owner,
            workspace=workspace,
            db_path=FM_DB_PATH,
            candidates=candidates,
        )
    except HexResolutionError as exc:
        raise ProjectPolicyBlocked(str(exc)) from exc
    if not result.get("allowed", False):
        findings = list(result.get("findings") or [])
        details = [
            str(detail)
            for finding in findings
            for detail in (finding.get("details") or [finding.get("message") or finding.get("henxel")])
            if detail
        ]
        raise ProjectPolicyBlocked(
            "active project policy blocked the candidate"
            + (": " + "; ".join(details[:8]) if details else ""),
            findings=findings,
        )
    return result


def _file_result(
    *,
    operation: str,
    path: str,
    kind: str,
    page: Dict[str, Any],
    range: Optional[Dict[str, Any]] = None,
    bytes_considered: Optional[int] = None,
    lines_considered: Optional[int] = None,
    truncation_reason: Optional[str] = None,
    encoding: Optional[str] = None,
    newline: Optional[str] = None,
    media_type: Optional[str] = None,
    fingerprint: Optional[str] = None,
    search_mode: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    diagnostics: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    return {
        "contract": _FILE_RESULT_CONTRACT,
        "operation": operation,
        "path": os.path.realpath(path),
        "kind": kind,
        "range": range,
        "page": page,
        "bytes_considered": bytes_considered,
        "lines_considered": lines_considered,
        "truncation_reason": truncation_reason,
        "encoding": encoding,
        "newline": newline,
        "media_type": media_type,
        "fingerprint": fingerprint,
        "search_mode": search_mode,
        "items": items or [],
        "diagnostics": diagnostics or [],
    }


def _with_file_result(result: Dict[str, Any], **contract: Any) -> Dict[str, Any]:
    result["metadata"] = {
        **(result.get("metadata") or {}),
        "file": _file_result(**contract),
    }
    return result


@dataclass(frozen=True)
class _TextSnapshot:
    text: str
    fingerprint: str
    encoding: str
    bom: bytes
    newline: str
    mode: int
    size: int


_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    bare_lf = text.count("\n") - crlf
    bare_cr = text.count("\r") - crlf
    if crlf >= bare_lf and crlf >= bare_cr and crlf:
        return "\r\n"
    if bare_cr > bare_lf:
        return "\r"
    return "\n"


def _read_text_snapshot(path: str) -> _TextSnapshot:
    raw = pathlib.Path(path).read_bytes()
    bom = b""
    encoding = "utf-8"
    payload = raw
    for marker, candidate in _BOMS:
        if raw.startswith(marker):
            bom, encoding, payload = marker, candidate, raw[len(marker):]
            break
    text = payload.decode(encoding)
    if "\x00" in text:
        index = text.index("\x00")
        raise UnicodeDecodeError(encoding, raw, index, index + 1, "NUL byte marks a binary file")
    return _TextSnapshot(
        text=text,
        fingerprint=fingerprint_bytes(raw),
        encoding=encoding,
        bom=bom,
        newline=_dominant_newline(text),
        mode=os.stat(path, follow_symlinks=False).st_mode,
        size=len(raw),
    )


def _encode_snapshot_text(snapshot: _TextSnapshot, text: str) -> bytes:
    return snapshot.bom + text.encode(snapshot.encoding)


def _preserve_newline_style(text: str, newline: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized if newline == "\n" else normalized.replace("\n", newline)


def _assert_expected_fingerprint(path: str, expected: Optional[str], actual: str) -> None:
    if expected is not None and expected != actual:
        raise AtomicWriteConflict(f"{path}: changed since the supplied fingerprint")


@dataclass(frozen=True)
class FileToolResources:
    reads: Tuple[str, ...] = ()
    writes: Tuple[str, ...] = ()


@dataclass(eq=False)
class _FileResourceRequest:
    resources: FileToolResources
    ready: asyncio.Future


def _canonical_resource_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _canonical_resources(
    *,
    reads: Tuple[str, ...] | List[str] = (),
    writes: Tuple[str, ...] | List[str] = (),
) -> FileToolResources:
    return FileToolResources(
        reads=tuple(sorted({_canonical_resource_path(path) for path in reads if path})),
        writes=tuple(sorted({_canonical_resource_path(path) for path in writes if path})),
    )


def _resource_paths_overlap(left: str, right: str) -> bool:
    try:
        common = os.path.commonpath([left, right])
    except ValueError:
        return False
    return common == left or common == right


def _resource_sets_conflict(left: FileToolResources, right: FileToolResources) -> bool:
    return any(
        _resource_paths_overlap(write, other)
        for write in left.writes
        for other in (*right.reads, *right.writes)
    ) or any(
        _resource_paths_overlap(read, write)
        for read in left.reads
        for write in right.writes
    )


class _FileResourceScheduler:
    def __init__(self) -> None:
        self._active: List[_FileResourceRequest] = []
        self._waiting: List[_FileResourceRequest] = []

    def _drain(self) -> None:
        for request in list(self._waiting):
            index = self._waiting.index(request)
            if any(_resource_sets_conflict(request.resources, active.resources) for active in self._active):
                continue
            if any(
                _resource_sets_conflict(request.resources, earlier.resources)
                for earlier in self._waiting[:index]
            ):
                continue
            self._waiting.remove(request)
            self._active.append(request)
            if not request.ready.done():
                request.ready.set_result(None)

    async def acquire(self, resources: FileToolResources) -> _FileResourceRequest:
        loop = asyncio.get_running_loop()
        request = _FileResourceRequest(resources, loop.create_future())
        self._waiting.append(request)
        self._drain()
        try:
            await request.ready
        except BaseException:
            if request in self._waiting:
                self._waiting.remove(request)
            if request in self._active:
                self._active.remove(request)
            self._drain()
            raise
        return request

    def release(self, request: _FileResourceRequest) -> None:
        if request in self._active:
            self._active.remove(request)
            self._drain()


_FILE_RESOURCE_SCHEDULERS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, _FileResourceScheduler]" = (
    weakref.WeakKeyDictionary()
)


def _file_resource_scheduler() -> _FileResourceScheduler:
    loop = asyncio.get_running_loop()
    scheduler = _FILE_RESOURCE_SCHEDULERS.get(loop)
    if scheduler is None:
        scheduler = _FileResourceScheduler()
        _FILE_RESOURCE_SCHEDULERS[loop] = scheduler
    return scheduler


@asynccontextmanager
async def schedule_file_resources(resources: FileToolResources):
    resources = _canonical_resources(reads=list(resources.reads), writes=list(resources.writes))
    if not resources.reads and not resources.writes:
        yield
        return
    scheduler = _file_resource_scheduler()
    request = await scheduler.acquire(resources)
    try:
        yield
    finally:
        scheduler.release(request)


def _tool_args(content: str) -> Dict[str, Any]:
    stripped = (content or "").strip()
    if not stripped.startswith("{"):
        return {}
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def file_tool_resources(tool: str, content: str, ctx: dict) -> FileToolResources:
    """Declare canonical file resources before a registered file tool runs."""
    from src.tool_execution import _resolve_search_root, _resolve_tool_path

    args = _tool_args(content)

    def resolve_file(raw: Any) -> Optional[str]:
        value = str(raw or "").strip()
        if not value:
            return None
        try:
            return _resolve_tool_path(value)
        except ValueError:
            workspace = str(ctx.get("workspace") or "").strip()
            expanded = os.path.expanduser(value)
            if workspace and not os.path.isabs(expanded):
                expanded = os.path.join(workspace, expanded)
            return os.path.realpath(expanded)

    def resolve_search(raw: Any = "") -> Optional[str]:
        try:
            return _resolve_search_root(str(raw or ""))
        except ValueError:
            return None

    if tool == "read_file":
        raw = args.get("path") if args else (content or "").split("\n", 1)[0]
        path = resolve_file(raw)
        return _canonical_resources(reads=[path] if path else [])

    if tool == "publish_file":
        from src.published_files import PublishedFileService

        raw = args.get("path") if args else (content or "").split("\n", 1)[0]
        path = resolve_file(raw)
        return _canonical_resources(
            reads=[path] if path else [],
            writes=[PublishedFileService().storage_root],
        )

    if tool in {"write_file", "edit_file"}:
        raw = args.get("path") if args else (content or "").split("\n", 1)[0]
        path = resolve_file(raw)
        paths = [path] if path else []
        return _canonical_resources(reads=paths, writes=paths)

    if tool == "apply_patch":
        patch_text = str(
            args.get("patch_text") or args.get("patchText") or args.get("patch") or ""
        ) if args else (content or "")
        try:
            operations = _parse_agent_patch(patch_text)
        except ValueError:
            return FileToolResources()
        reads: List[str] = []
        writes: List[str] = []
        for operation in operations:
            path = resolve_file(operation.get("path"))
            if not path:
                continue
            writes.append(path)
            if operation.get("kind") != "add":
                reads.append(path)
        return _canonical_resources(reads=reads, writes=writes)

    if tool in {"ls", "glob", "grep"}:
        raw = args.get("path", "") if args else (content if tool == "ls" else "")
        root = resolve_search(raw)
        return _canonical_resources(reads=[root] if root else [])

    if tool == "manage_files":
        try:
            root, _, workspace = _file_trash_scope(ctx)
        except ValueError:
            return FileToolResources()
        action = str(args.get("action") or "").strip().lower()
        root_path = str(root)
        if action in {"list", "list_trash", "trash"}:
            return _canonical_resources(reads=[root_path])
        if action == "restore":
            trash_id = str(args.get("trash_id") or args.get("id") or "").strip()
            manifest = str(root / f"{trash_id}.json")
            payload = str(root / f"{trash_id}.data")
            destination = resolve_file(args.get("path")) if args.get("path") else workspace
            return _canonical_resources(
                reads=[manifest, payload],
                writes=[manifest, payload, destination, root_path],
            )
        source = resolve_file(args.get("path") or args.get("source"))
        if action == "move":
            destination = resolve_file(args.get("destination") or args.get("to"))
            return _canonical_resources(
                reads=[source] if source else [],
                writes=[path for path in (source, destination) if path],
            )
        if action == "delete":
            return _canonical_resources(
                reads=[source] if source else [],
                writes=[path for path in (source, root_path) if path],
            )

    return FileToolResources()


def scheduled_file_handler(
    tool: str,
    handler: Callable[[str, dict], Awaitable[dict]],
) -> Callable[[str, dict], Awaitable[dict]]:
    @functools.wraps(handler)
    async def wrapped(content: str, ctx: dict) -> dict:
        resources = file_tool_resources(tool, content, ctx)
        async with schedule_file_resources(resources):
            return await handler(content, ctx)

    return wrapped


class PublishFileTool:
    """Copy one allowed local file into managed storage and issue a grant."""

    async def execute(self, content: str, ctx: dict) -> dict:
        from src.published_files import PublishedFileError, PublishedFileService
        from src.settings import get_setting
        from src.tool_execution import (
            _is_control_data_path,
            _is_sensitive_path,
            _resolve_tool_path,
            get_active_workspace,
        )
        from src.tool_security import owner_is_admin_or_single_user

        try:
            args = json.loads(content) if content.strip().startswith("{") else {"path": content.strip()}
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        raw_path = str(args.get("path") or "").strip()
        if not raw_path:
            return {"error": "publish_file: path required", "exit_code": 1}

        # Reject a final symlink even when it resolves inside an allowed root;
        # the caller should name the real file it intends to publish.
        workspace = get_active_workspace()
        lexical = os.path.expanduser(raw_path)
        if workspace and not os.path.isabs(lexical):
            lexical = os.path.join(workspace, lexical)
        if os.path.islink(os.path.abspath(lexical)):
            return {"error": "publish_file: the source may not be a symlink", "exit_code": 1}

        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as confined_error:
            # Admins on a self-host can publish from their own home directory,
            # which is the explicit local-PC use case. Regular users remain
            # confined to the workspace/root policy shared by all file tools.
            owner = str(ctx.get("owner") or "").strip().lower()
            if not owner_is_admin_or_single_user(owner):
                return {"error": f"publish_file: {confined_error}", "exit_code": 1}
            path = os.path.realpath(os.path.expanduser(raw_path))
            home = os.path.realpath(str(pathlib.Path.home()))
            try:
                inside_home = os.path.commonpath([os.path.normcase(path), os.path.normcase(home)]) == os.path.normcase(home)
            except ValueError:
                inside_home = False
            if not inside_home or _is_sensitive_path(path) or _is_control_data_path(path):
                return {"error": f"publish_file: {confined_error}", "exit_code": 1}

        owner = str(ctx.get("owner") or "").strip().lower()
        try:
            result = await asyncio.to_thread(
                PublishedFileService().publish,
                path,
                owner=owner,
                audience=str(args.get("audience") or "owner"),
                expires_in_hours=args.get("expires_in_hours"),
                public_origin=os.getenv("APP_PUBLIC_URL") or get_setting("app_public_url"),
            )
        except (PublishedFileError, OSError, ValueError) as exc:
            return {"error": f"publish_file: {exc}", "exit_code": 1}

        url = result["download_url"]
        return {
            "output": f"Published [{result['filename']}]({url})\nBreak or replace this link from Files.",
            "exit_code": 0,
            "file_id": result["id"],
            "filename": result["filename"],
            "download_url": url,
            "audience": result["audience"],
            "expires_at": result["expires_at"],
        }


def _file_trash_scope(ctx: dict) -> tuple[pathlib.Path, str, str]:
    from src.constants import DATA_DIR
    from src.tool_execution import get_active_workspace

    owner = str(ctx.get("owner") or "").strip().lower()
    if not owner:
        raise ValueError("manage_files requires an authenticated owner")
    raw_workspace = str(ctx.get("workspace") or get_active_workspace() or "").strip()
    if not raw_workspace:
        raise ValueError("manage_files requires an active workspace")
    workspace = os.path.realpath(raw_workspace)
    owner_key = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:20]
    workspace_key = hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:20]
    root = pathlib.Path(DATA_DIR) / "file-trash" / owner_key / workspace_key
    return root, owner, workspace


_FILE_MUTATION_PERMISSION = "native-file-mutation"


@dataclass(frozen=True)
class _PendingFileApproval:
    owner: str
    session_id: str
    workspace: str
    authority_workspace_id: str
    future: asyncio.Future


_PENDING_FILE_APPROVALS: Dict[str, _PendingFileApproval] = {}
_PENDING_FILE_APPROVALS_LOCK = threading.Lock()


def file_approval_binding(
    action: str,
    *,
    workspace: str,
    source: str,
    destination: str = "",
    trash_target: str = "",
    trash_manifest: str = "",
    source_fingerprint: str = "",
) -> str:
    descriptor = {
        "action": str(action),
        "workspace": os.path.realpath(workspace),
        "source": os.path.realpath(source),
        "destination": os.path.realpath(destination) if destination else "",
        "trash_target": os.path.realpath(trash_target) if trash_target else "",
        "trash_manifest": os.path.realpath(trash_manifest) if trash_manifest else "",
        "source_fingerprint": str(source_fingerprint),
    }
    payload = json.dumps(
        descriptor,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _require_file_approval(
    action: str,
    *,
    ctx: dict,
    owner: str,
    workspace: str,
    source: str,
    destination: str = "",
    trash_target: str = "",
    trash_manifest: str = "",
    source_fingerprint: str = "",
) -> str:
    session_id = str(ctx.get("session_id") or "")
    authority_workspace_id = str(
        ctx.get("authority_workspace_id") or ""
    ).strip()
    from src.openclank.operation_approvals import canonical_workspace_id
    authority_workspace_id = canonical_workspace_id(owner, authority_workspace_id)
    if not session_id:
        raise PermissionError("file mutations require a trusted session")
    binding = file_approval_binding(
        action,
        workspace=workspace,
        source=source,
        destination=destination,
        trash_target=trash_target,
        trash_manifest=trash_manifest,
        source_fingerprint=source_fingerprint,
    )
    try:
        from src.openclank.operation_approvals import match_operation_approval

        if match_operation_approval(
            owner=owner,
            permission_type=_FILE_MUTATION_PERMISSION,
            resource=binding,
            session_id=session_id,
            workspace_id=authority_workspace_id,
            workspace_path=workspace,
        ):
            return binding
    except Exception:
        pass
    # Owner permission mode (yolo/auto): approve with once semantics — no
    # durable grant is written.
    try:
        from src.permission_mode import auto_approves

        if auto_approves(owner):
            return binding
    except Exception:
        pass

    progress_cb = ctx.get("progress_cb")
    if not callable(progress_cb):
        raise PermissionError("file mutation needs interactive approval")

    request_id = "file_perm_" + uuid.uuid4().hex[:20]
    future = asyncio.get_running_loop().create_future()
    pending = _PendingFileApproval(
        owner=owner,
        session_id=session_id,
        workspace=workspace,
        authority_workspace_id=authority_workspace_id,
        future=future,
    )
    with _PENDING_FILE_APPROVALS_LOCK:
        _PENDING_FILE_APPROVALS[request_id] = pending
    detail = {
        key: value
        for key, value in {
            "action": action,
            "source": source,
            "destination": destination,
            "trash_target": trash_target,
            "trash_manifest": trash_manifest,
            "source_fingerprint": source_fingerprint,
        }.items()
        if value
    }
    try:
        await progress_cb({
            "type": "permission_request",
            "data": {
                "request_id": request_id,
                "session_id": session_id,
                "permission_type": f"manage_files {action} · exact {binding[:12]}",
                "detail": detail,
                "options": ["once", "chat", "workspace", "always", "reject"],
                "always_pattern": "*",
            },
        })
        option_id = await future
        if option_id in {"chat", "workspace", "always"}:
            try:
                from src.openclank.permission_grants import grant_scope_for_lifetime
                (
                    grant_session,
                    grant_workspace,
                    grant_workspace_id,
                ) = grant_scope_for_lifetime(
                    option_id,
                    session_id=session_id,
                    workspace=workspace,
                    workspace_id=authority_workspace_id,
                )
                if (
                    option_id == "always"
                    or grant_session
                    or grant_workspace
                    or grant_workspace_id
                ):
                    from src.openclank.operation_approvals import (
                        record_operation_approval,
                    )

                    persisted = record_operation_approval(
                        owner=owner,
                        permission_type=_FILE_MUTATION_PERMISSION,
                        pattern="*",
                        resource=binding,
                        lifetime=option_id,
                        session_id=grant_session,
                        workspace_id=grant_workspace_id,
                        target_path=workspace,
                    )
                    if not persisted:
                        option_id = "once"
                else:
                    option_id = "once"
            except Exception as exc:
                raise PermissionError(
                    "could not persist the bound file approval"
                ) from exc
        if option_id not in {"once", "chat", "workspace", "always"}:
            raise PermissionError("file mutation was rejected")
        return binding
    finally:
        with _PENDING_FILE_APPROVALS_LOCK:
            _PENDING_FILE_APPROVALS.pop(request_id, None)


def resolve_file_approval(
    request_id: str,
    option_id: str,
    *,
    owner: str,
    session_id: str,
) -> bool:
    if option_id not in {"once", "chat", "workspace", "always", "reject"}:
        return False
    with _PENDING_FILE_APPROVALS_LOCK:
        pending = _PENDING_FILE_APPROVALS.get(str(request_id))
        if (
            pending is None
            or pending.owner != str(owner or "")
            or pending.session_id != str(session_id or "")
            or pending.future.done()
        ):
            return False
        future = pending.future

    def finish() -> None:
        if not future.done():
            future.set_result(option_id)

    future.get_loop().call_soon_threadsafe(finish)
    return True


def reject_file_approval_scope(
    *,
    owner: str,
    session_id: str = "",
    workspace: str = "",
    authority_workspace_id: str = "",
    all_pending: bool = False,
) -> int:
    """Reject pending file approvals in one stable reset domain."""
    if (
        not session_id
        and not workspace
        and not authority_workspace_id
        and not all_pending
    ):
        return 0
    matches: list[asyncio.Future] = []
    with _PENDING_FILE_APPROVALS_LOCK:
        for pending in _PENDING_FILE_APPROVALS.values():
            if pending.owner != str(owner or "") or pending.future.done():
                continue
            if all_pending:
                matches.append(pending.future)
                continue
            if session_id and pending.session_id != str(session_id):
                continue
            if workspace and authority_workspace_id:
                if (
                    pending.authority_workspace_id != authority_workspace_id
                    and pending.workspace != workspace
                ):
                    continue
            elif workspace and pending.workspace != workspace:
                continue
            elif (
                authority_workspace_id
                and pending.authority_workspace_id != authority_workspace_id
            ):
                continue
            matches.append(pending.future)
    for future in matches:
        future.get_loop().call_soon_threadsafe(
            lambda target=future: (
                None if target.done() else target.set_result("reject")
            )
        )
    return len(matches)


class ManageFilesTool:
    """Conflict-safe move plus recoverable delete/restore for regular files."""

    async def execute(self, content: str, ctx: dict) -> dict:
        if _agent_service_enabled(ctx):
            try:
                args = json.loads(content) if (content or "").strip().startswith("{") else {}
            except (json.JSONDecodeError, TypeError):
                args = {}
            if not isinstance(args, dict):
                args = {}
            action = str(args.get("action") or "").strip().lower()
            if action in {"list", "list_trash"}:
                return {
                    "error": "manage_files: list_trash is not yet exposed by the Rust service lane",
                    "exit_code": 1,
                    "code": "unsupported",
                }
            try:
                from src.tool_execution import get_active_workspace

                owner = str(ctx.get("owner") or "").strip()
                workspace = str(
                    get_active_workspace() or ctx.get("workspace") or ""
                ).strip()
                if not owner or not workspace:
                    raise ValueError(
                        "mutations require an authenticated owner and active workspace"
                    )
                if action in {"move", "delete", "trash"}:
                    raw_source = str(args.get("path") or args.get("source") or "").strip()
                    if not raw_source:
                        raise ValueError(f"{action} requires path")
                    source = _agent_target_path(raw_source)
                    error, fingerprint = await _agent_mutation_fingerprint(ctx, source)
                    if error is not None:
                        return error
                    supplied = args.get("expected_fingerprint")
                    if supplied is not None and str(supplied) != fingerprint:
                        return {
                            "error": f"manage_files: conflict: {source} changed since the supplied fingerprint",
                            "exit_code": 1,
                            "code": "conflict",
                        }
                    expected = {
                        "algorithm": "sha256",
                        "value": fingerprint,
                    }
                    if action == "move":
                        raw_destination = str(args.get("destination") or args.get("to") or "").strip()
                        if not raw_destination:
                            raise ValueError("move requires destination")
                        destination = _agent_target_path(raw_destination)
                        await _require_file_approval(
                            "move",
                            ctx=ctx,
                            owner=owner,
                            workspace=workspace,
                            source=source,
                            destination=destination,
                            source_fingerprint=fingerprint or "",
                        )
                        response = await _agent_request(ctx, "manage_files", "move", source, {
                            "destination": destination,
                            "expected_fingerprint": expected,
                        })
                        if response is None or response.get("code"):
                            return response
                        return {"output": f"Moved {source} to {destination}", "exit_code": 0, "source": source, "path": destination}
                    await _require_file_approval(
                        "trash",
                        ctx=ctx,
                        owner=owner,
                        workspace=workspace,
                        source=source,
                        source_fingerprint=fingerprint or "",
                    )
                    response = await _agent_request(ctx, "manage_files", "trash", source, {
                        "expected_fingerprint": expected,
                    })
                    if response is None or response.get("code"):
                        return response
                    entry = _agent_data(response)
                    return {
                        "output": f"Moved {source} to recoverable trash.",
                        "exit_code": 0,
                        "trash_id": entry.get("id"),
                        "root_id": entry.get("root_id"),
                        "original_path": entry.get("original_path") or source,
                        "trashed_path": entry.get("trashed_path"),
                        "fingerprint": entry.get("fingerprint") or fingerprint,
                        "recoverable": True,
                    }
                if action == "restore":
                    entry = args.get("entry")
                    if not isinstance(entry, dict):
                        return {
                            "error": "manage_files: restore requires the server-issued entry object",
                            "exit_code": 1,
                            "code": "unsupported",
                        }
                    original = _agent_target_path(str(entry.get("original_path") or ""))
                    trashed = _agent_target_path(str(entry.get("trashed_path") or ""))
                    error, fingerprint = await _agent_mutation_fingerprint(ctx, trashed)
                    if error is not None:
                        return error
                    await _require_file_approval(
                        "restore",
                        ctx=ctx,
                        owner=owner,
                        workspace=workspace,
                        source=trashed,
                        destination=original,
                        trash_target=trashed,
                        source_fingerprint=fingerprint or "",
                    )
                    response = await _agent_request(ctx, "manage_files", "restore", original, {
                        "entry": entry,
                        "expected_fingerprint": {
                            "algorithm": "sha256",
                            "value": fingerprint,
                        },
                    })
                    if response is None or response.get("code"):
                        return response
                    return {"output": f"Restored {original}", "exit_code": 0, "path": original}
                return {"error": "manage_files: action must be move, trash, restore, or list_trash", "exit_code": 1}
            except ValueError as exc:
                return {"error": f"manage_files: {exc}", "exit_code": 1}
        from src.tool_execution import _resolve_tool_path

        try:
            args = json.loads(content) if (content or "").strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        action = str(args.get("action") or "").strip().lower()
        try:
            root, owner, workspace = _file_trash_scope(ctx)
        except ValueError as exc:
            return {"error": f"manage_files: {exc}", "exit_code": 1}
        history_context = _history_context(ctx)

        if action in {"list", "list_trash", "trash"}:
            try:
                cursor = max(0, int(args.get("cursor") or 0))
                limit = max(1, min(int(args.get("limit") or 20), 100))
            except (TypeError, ValueError):
                cursor, limit = 0, 20

            def _list():
                rows = []
                if root.is_dir():
                    for path in root.glob("*.json"):
                        try:
                            row = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, ValueError, TypeError):
                            continue
                        if (
                            isinstance(row, dict)
                            and row.get("owner") == owner
                            and row.get("workspace") == workspace
                        ):
                            rows.append(row)
                rows.sort(key=lambda row: float(row.get("deleted_at") or 0), reverse=True)
                return rows

            rows = await asyncio.to_thread(_list)
            page = rows[cursor:cursor + limit]
            next_cursor = cursor + len(page) if cursor + len(page) < len(rows) else None
            return {
                "output": (
                    "Trash is empty."
                    if not page
                    else "\n".join(
                        f"[{row.get('id')}] {row.get('original_path')} ({row.get('size')} B)"
                        for row in page
                    )
                ),
                "exit_code": 0,
                "items": page,
                "page": {
                    "cursor": cursor,
                    "next_cursor": next_cursor,
                    "has_more": next_cursor is not None,
                    "total": len(rows),
                },
                "owner": owner,
                "workspace": workspace,
            }

        if action not in {"move", "delete", "restore"}:
            return {
                "error": "manage_files: action must be move, delete, restore, or list_trash",
                "exit_code": 1,
            }

        def _lexical(raw: str) -> str:
            expanded = os.path.expanduser(raw)
            if workspace and not os.path.isabs(expanded):
                expanded = os.path.join(workspace, expanded)
            return os.path.abspath(expanded)

        try:
            if action == "restore":
                trash_id = str(args.get("trash_id") or args.get("id") or "").strip()
                if not re.fullmatch(r"[a-f0-9]{24}", trash_id):
                    raise ValueError("restore requires a valid trash_id")
                meta_path = root / f"{trash_id}.json"
                payload_path = root / f"{trash_id}.data"
                meta_raw = meta_path.read_bytes()
                metadata = json.loads(meta_raw.decode("utf-8"))
                if (
                    metadata.get("owner") != owner
                    or metadata.get("workspace") != workspace
                ):
                    raise AtomicWriteConflict("trashed file belongs to another workspace owner")
                if float(metadata.get("expires_at") or 0) < time.time():
                    raise AtomicWriteConflict("trashed file recovery window has expired")
                payload = payload_path.read_bytes()
                if fingerprint_bytes(payload) != metadata.get("fingerprint"):
                    raise AtomicWriteConflict("trashed payload fingerprint does not match its manifest")
                raw_destination = str(args.get("path") or metadata.get("original_path") or "").strip()
                destination = _resolve_tool_path(raw_destination)
                if os.path.lexists(_lexical(raw_destination)):
                    raise AtomicWriteConflict(f"{destination}: restore destination already exists")
                await _require_file_approval(
                    "restore",
                    ctx=ctx,
                    owner=owner,
                    workspace=workspace,
                    source=str(payload_path),
                    destination=destination,
                    trash_target=str(payload_path),
                    trash_manifest=str(meta_path),
                    source_fingerprint=fingerprint_bytes(payload),
                )
                policy = await asyncio.to_thread(
                    _validate_file_policy, ctx, {destination: payload}
                )
                atomic_write_batch([
                    AtomicFileChange(
                        destination,
                        payload,
                        require_missing=True,
                        mode=int(metadata.get("mode") or 0o644),
                        history_context=history_context,
                    ),
                    AtomicFileChange(
                        str(payload_path),
                        None,
                        expected_fingerprint=fingerprint_bytes(payload),
                        history_context=history_context,
                    ),
                    AtomicFileChange(
                        str(meta_path),
                        None,
                        expected_fingerprint=fingerprint_bytes(meta_raw),
                        history_context=history_context,
                    ),
                ])
                result = {
                    "output": f"Restored {destination}",
                    "exit_code": 0,
                    "path": destination,
                    "fingerprint": fingerprint_bytes(payload),
                    "history": last_history_status(history_context),
                    "owner": owner,
                    "workspace": workspace,
                }
                if policy.get("warnings"):
                    result["policy_warnings"] = policy["warnings"]
                return result

            raw_source = str(args.get("path") or args.get("source") or "").strip()
            if not raw_source:
                raise ValueError(f"{action} requires path")
            if os.path.islink(_lexical(raw_source)):
                raise ValueError("symbolic links cannot be moved or deleted by manage_files")
            source = _resolve_tool_path(raw_source)
            if not os.path.isfile(source):
                raise ValueError(f"{source}: not a regular file")
            payload = pathlib.Path(source).read_bytes()
            observed = fingerprint_bytes(payload)
            expected = args.get("expected_fingerprint")
            if expected is not None and str(expected) != observed:
                raise AtomicWriteConflict(f"{source}: changed since the supplied fingerprint")
            mode = os.stat(source, follow_symlinks=False).st_mode

            if action == "move":
                raw_destination = str(args.get("destination") or args.get("to") or "").strip()
                if not raw_destination:
                    raise ValueError("move requires destination")
                if os.path.lexists(_lexical(raw_destination)):
                    raise AtomicWriteConflict(f"{raw_destination}: destination already exists")
                destination = _resolve_tool_path(raw_destination)
                if source == destination:
                    raise ValueError("source and destination are the same file")
                await _require_file_approval(
                    "move",
                    ctx=ctx,
                    owner=owner,
                    workspace=workspace,
                    source=source,
                    destination=destination,
                    source_fingerprint=observed,
                )
                policy = await asyncio.to_thread(
                    _validate_file_policy,
                    ctx,
                    {source: None, destination: payload},
                )
                atomic_write_batch([
                    AtomicFileChange(destination, payload, require_missing=True, mode=mode, history_context=history_context),
                    AtomicFileChange(source, None, expected_fingerprint=observed, mode=mode, history_context=history_context),
                ])
                result = {
                    "output": f"Moved {source} to {destination}",
                    "exit_code": 0,
                    "source": source,
                    "path": destination,
                    "fingerprint": observed,
                    "history": last_history_status(history_context),
                    "owner": owner,
                    "workspace": workspace,
                }
                if policy.get("warnings"):
                    result["policy_warnings"] = policy["warnings"]
                return result

            trash_id = uuid.uuid4().hex[:24]
            payload_path = root / f"{trash_id}.data"
            meta_path = root / f"{trash_id}.json"
            await _require_file_approval(
                "delete",
                ctx=ctx,
                owner=owner,
                workspace=workspace,
                source=source,
                trash_target=str(payload_path),
                trash_manifest=str(meta_path),
                source_fingerprint=observed,
            )
            policy = await asyncio.to_thread(
                _validate_file_policy, ctx, {source: None}
            )
            root.mkdir(parents=True, exist_ok=True)
            metadata = {
                "id": trash_id,
                "original_path": source,
                "deleted_at": time.time(),
                "expires_at": time.time() + (30 * 24 * 60 * 60),
                "owner": owner,
                "workspace": workspace,
                "fingerprint": observed,
                "mode": mode,
                "size": len(payload),
            }
            meta_raw = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
            atomic_write_batch([
                AtomicFileChange(str(payload_path), payload, require_missing=True, mode=0o600, history_context=history_context),
                AtomicFileChange(str(meta_path), meta_raw, require_missing=True, mode=0o600, history_context=history_context),
                AtomicFileChange(source, None, expected_fingerprint=observed, mode=mode, history_context=history_context),
            ])
            result = {
                "output": f"Moved {source} to recoverable trash `{trash_id}`.",
                "exit_code": 0,
                "trash_id": trash_id,
                "original_path": source,
                "fingerprint": observed,
                "history": last_history_status(history_context),
                "recoverable": True,
                "owner": owner,
                "workspace": workspace,
            }
            if policy.get("warnings"):
                result["policy_warnings"] = policy["warnings"]
            return result
        except FileNotFoundError as exc:
            return {"error": f"manage_files: {exc}: not found", "exit_code": 1}
        except AtomicWriteConflict as exc:
            return {
                "error": f"manage_files: conflict: {exc}. Read/list again and retry.",
                "exit_code": 1,
                "conflict": True,
            }
        except ProjectPolicyBlocked as exc:
            return {
                "error": f"manage_files: {exc}",
                "exit_code": 1,
                "blocked": True,
                "policy_findings": exc.findings,
            }
        except (ValueError, PermissionError, OSError, json.JSONDecodeError) as exc:
            return {"error": f"manage_files: {exc}", "exit_code": 1}


def _glob_to_regex(pat: str) -> "re.Pattern":
    """Translate a forward-slash glob (**, *, ?) into a compiled regex.
    `**/` matches zero or more complete directories.
    `*` matches within a single path segment (does not cross /).
    """
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i : i + 3] == "**/":
            out.append("(?:[^/]+/)*")
            i += 3
        elif pat[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("".join(out))

def _unified_diff(old: str, new: str, path: str) -> Optional[Dict[str, Any]]:
    if old == new:
        return None
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    label = path or "file"
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{label}", tofile=f"b/{label}",
        lineterm="",
    ))
    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))
    truncated = False
    if len(diff_lines) > MAX_DIFF_LINES:
        diff_lines = diff_lines[:MAX_DIFF_LINES]
        truncated = True
    text = "\n".join(diff_lines)
    if truncated:
        text += f"\n… diff truncated at {MAX_DIFF_LINES} lines"
    return {
        "text": text,
        "added": added,
        "removed": removed,
        "new_file": old == "",
        "file": os.path.basename(path) or (path or "file"),
    }

class EditFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_edit_file(content, ctx)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        try:
            args = json.loads(content) if content.strip().startswith("{") else {}
        except (json.JSONDecodeError, TypeError):
            args = {}
        raw_path = (args.get("path") or "").strip()
        old = args.get("old_string", "")
        new = args.get("new_string", "")
        replace_all = bool(args.get("replace_all", False))
        expected_fingerprint = args.get("expected_fingerprint")
        if expected_fingerprint is not None:
            expected_fingerprint = str(expected_fingerprint)
        if not raw_path:
            return {"error": "edit_file: path required", "exit_code": 1}
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"edit_file: {e}", "exit_code": 1}
        history_context = _history_context(ctx)
        if old == "":
            return {"error": "edit_file: old_string required (use write_file to create a file)", "exit_code": 1}
        if old == new:
            return {"error": "edit_file: old_string and new_string are identical", "exit_code": 1}

        def _apply():
            """Helper function that performs the actual string replacement and file writing logic."""
            snapshot = _read_text_snapshot(path)
            _assert_expected_fingerprint(path, expected_fingerprint, snapshot.fingerprint)
            original = snapshot.text
            count = original.count(old)
            if count == 0:
                return original, None, "not_found"
            if count > 1 and not replace_all:
                return original, None, f"not_unique:{count}"
            updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
            # Re-resolve immediately before commit so a swapped parent/final
            # symlink cannot silently redirect the staged replacement.
            if _resolve_tool_path(path) != path:
                raise AtomicWriteConflict(f"{path}: canonical target changed during edit")
            data = _encode_snapshot_text(snapshot, updated)
            policy = _validate_file_policy(ctx, {path: data})
            fingerprint = atomic_write_bytes(
                path,
                data,
                expected_fingerprint=snapshot.fingerprint,
                mode=snapshot.mode,
                history_context=history_context,
            )
            return (
                original,
                updated,
                "ok",
                snapshot.fingerprint,
                fingerprint,
                len(data),
                snapshot.encoding,
                snapshot.newline,
                policy,
            )

        try:
            applied = await asyncio.to_thread(_apply)
        except FileNotFoundError:
            return {"error": f"edit_file: {path}: not found (use write_file to create it)", "exit_code": 1}
        except (IsADirectoryError, UnicodeDecodeError):
            return {"error": f"edit_file: {path}: not an editable text file", "exit_code": 1}
        except AtomicWriteConflict as e:
            return {"error": f"edit_file: conflict: {e}. Read the file again and retry.", "exit_code": 1, "conflict": True}
        except ProjectPolicyBlocked as e:
            return {
                "error": f"edit_file: {e}",
                "exit_code": 1,
                "blocked": True,
                "policy_findings": e.findings,
            }
        except PermissionError:
            return {"error": f"edit_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"edit_file: {path}: {e}", "exit_code": 1}
        if len(applied) == 3:
            original, updated, status = applied
            old_fingerprint = new_fingerprint = None
            size = 0
            encoding = "utf-8"
            newline = "\n"
            policy = {"warnings": []}
        else:
            (
                original,
                updated,
                status,
                old_fingerprint,
                new_fingerprint,
                size,
                encoding,
                newline,
                policy,
            ) = applied

        if status == "not_found":
            return {"error": f"edit_file: old_string not found in {path}. Read the file and match it exactly.", "exit_code": 1}
        if status.startswith("not_unique"):
            n = status.split(":", 1)[1]
            return {"error": f"edit_file: old_string is not unique in {path} ({n} matches). Add surrounding context or set replace_all=true.", "exit_code": 1}

        n = original.count(old)
        result = {
            "output": f"Edited {path} ({n} replacement{'s' if n != 1 else ''})",
            "exit_code": 0,
            "path": path,
            "old_fingerprint": old_fingerprint,
            "fingerprint": new_fingerprint,
            "history": last_history_status(history_context),
        }
        diff = _unified_diff(original, updated, path)
        if diff:
            result["diff"] = diff
        if policy.get("warnings"):
            result["policy_warnings"] = policy["warnings"]
        return _with_file_result(
            result,
            operation="edit",
            path=path,
            kind="text",
            range={"unit": "byte", "start": 0, "end": size - 1} if size else None,
            page={
                "unit": "byte",
                "cursor": 0,
                "next_cursor": None,
                "has_more": False,
                "returned": size,
                "total": size,
            },
            bytes_considered=size,
            lines_considered=len(updated.splitlines()),
            encoding=encoding,
            newline={"\n": "lf", "\r\n": "crlf", "\r": "cr"}[newline],
            media_type="text/plain",
            fingerprint=new_fingerprint,
        )

class ReadFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_read_file(content, ctx)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        raw_path, offset, limit, cursor = content.split("\n", 1)[0].strip(), 0, 0, 0
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                raw_path = str(_a.get("path", "")).strip()
                offset = int(_a.get("offset") or 0)
                limit = int(_a.get("limit") or 0)
                cursor = max(0, int(_a.get("cursor") or 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"read_file: {e}", "exit_code": 1}
        try:
            def _read():
                snapshot = _read_text_snapshot(path)
                all_lines = snapshot.text.splitlines(keepends=True)
                total_lines = len(all_lines)
                next_offset = None
                has_more = False
                if offset > 0 or limit > 0:
                    start = max(offset, 1)
                    out, n, budget = [], 0, MAX_READ_CHARS
                    hit_character_limit = False
                    for i, line in enumerate(all_lines, 1):
                        if i < start:
                            continue
                        if limit > 0 and n >= limit:
                            break
                        out.append(line)
                        n += 1
                        budget -= len(line)
                        if budget <= 0:
                            out.append(f"\n... [truncated at {MAX_READ_CHARS} chars]")
                            hit_character_limit = True
                            break
                    data = "".join(out)
                    next_line = start + n
                    has_more = next_line <= total_lines
                    next_offset = next_line if has_more else None
                    next_cursor = None
                    truncation_reason = (
                        "character_limit"
                        if has_more and hit_character_limit
                        else "line_limit" if has_more else None
                    )
                else:
                    data = snapshot.text[cursor:cursor + MAX_READ_CHARS]
                    has_more = cursor + len(data) < len(snapshot.text)
                    next_cursor = cursor + len(data) if has_more else None
                    truncation_reason = "character_limit" if has_more else None
                return (
                    data,
                    snapshot,
                    total_lines,
                    next_offset,
                    next_cursor,
                    has_more,
                    len(data.splitlines()),
                    truncation_reason,
                )
            (
                data,
                snapshot,
                total_lines,
                next_offset,
                next_cursor,
                has_more,
                lines_returned,
                truncation_reason,
            ) = await asyncio.to_thread(_read)
        except FileNotFoundError:
            return {"error": f"read_file: {path}: not found", "exit_code": 1}
        except PermissionError:
            return {"error": f"read_file: {path}: permission denied", "exit_code": 1}
        except IsADirectoryError:
            return {"error": f"read_file: {path}: is a directory (use ls)", "exit_code": 1}
        except UnicodeDecodeError:
            try:
                size = os.path.getsize(path)
                fingerprint = file_fingerprint(path)
            except OSError:
                size = None
                fingerprint = None
            return _with_file_result({
                "error": f"read_file: {path}: unsupported text encoding or binary file",
                "exit_code": 1,
            }, operation="read", path=path, kind="binary", page={
                "unit": "byte",
                "cursor": 0,
                "next_cursor": None,
                "has_more": False,
                "returned": 0,
                "total": size,
            }, bytes_considered=size, media_type="application/octet-stream",
                fingerprint=fingerprint, diagnostics=[{
                    "code": "unsupported_media",
                    "message": "Native read_file accepts supported text encodings only.",
                }])
        except OSError as e:
            return {"error": f"read_file: {path}: {e}", "exit_code": 1}
        returned = lines_returned if (offset > 0 or limit > 0) else len(data)
        page_cursor = max(offset, 1) if (offset > 0 or limit > 0) else cursor
        page_total = total_lines if (offset > 0 or limit > 0) else len(snapshot.text)
        page_unit = "line" if (offset > 0 or limit > 0) else "character"
        contract_next = next_offset if page_unit == "line" else next_cursor
        if not (offset > 0 or limit > 0) and has_more:
            data += f"\n... [truncated at {MAX_READ_CHARS} chars; continue at cursor {next_cursor}]"
        return _with_file_result({
            "output": data,
            "exit_code": 0,
            "path": path,
            "fingerprint": snapshot.fingerprint,
            "encoding": snapshot.encoding,
            "media_type": "text/plain",
            "newline": {"\n": "lf", "\r\n": "crlf", "\r": "cr"}[snapshot.newline],
            "bytes_considered": snapshot.size,
            "lines_considered": lines_returned,
            "truncation_reason": truncation_reason,
            "page": {
                "offset": max(offset, 1) if (offset > 0 or limit > 0) else 1,
                "limit": limit or None,
                "next_offset": next_offset,
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": has_more,
                "total_lines": total_lines,
            },
        }, operation="read", path=path, kind="text", range={
            "unit": page_unit,
            "start": page_cursor,
            "end": page_cursor + returned - 1,
        } if returned else None, page={
            "unit": page_unit,
            "cursor": page_cursor,
            "next_cursor": contract_next,
            "has_more": has_more,
            "returned": returned,
            "total": page_total,
        }, bytes_considered=snapshot.size, lines_considered=lines_returned,
            truncation_reason=truncation_reason, encoding=snapshot.encoding,
            newline={"\n": "lf", "\r\n": "crlf", "\r": "cr"}[snapshot.newline],
            media_type="text/plain", fingerprint=snapshot.fingerprint,
            diagnostics=[{
                "code": "truncated",
                "message": f"Continue at {page_unit} cursor {contract_next}.",
            }] if has_more else [])

class WriteFileTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_write_file(content, ctx)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import _resolve_tool_path, _resolve_search_root, _truncate
        lines = content.split("\n", 1)
        raw_path = lines[0].strip()
        body = lines[1] if len(lines) > 1 else ""
        expected_fingerprint: Optional[str] = None
        # Decode JSON-object args (the fenced inline-args shape
        # ```write_file {"path": "...", "content": "..."}```), matching
        # ReadFileTool above. Without this the whole JSON string becomes the
        # path and the file is written under a garbage name. This is the live
        # path: there is no filesystem MCP server, so write_file always runs
        # here via _direct_fallback, not through _build_mcp_args.
        _stripped = content.strip()
        if _stripped.startswith("{"):
            try:
                _a = json.loads(_stripped)
                if isinstance(_a, dict) and "path" in _a:
                    raw_path = str(_a.get("path", "")).strip()
                    body = str(_a.get("content", ""))
                    if _a.get("expected_fingerprint") is not None:
                        expected_fingerprint = str(_a["expected_fingerprint"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        try:
            path = _resolve_tool_path(raw_path)
        except ValueError as e:
            return {"error": f"write_file: {e}", "exit_code": 1}
        history_context = _history_context(ctx)
        try:
            def _write():
                old = ""
                old_fingerprint = None
                snapshot: Optional[_TextSnapshot] = None
                try:
                    snapshot = _read_text_snapshot(path)
                    old = snapshot.text
                    old_fingerprint = snapshot.fingerprint
                except FileNotFoundError:
                    snapshot = None
                if snapshot is not None:
                    _assert_expected_fingerprint(path, expected_fingerprint, snapshot.fingerprint)
                    rendered = _preserve_newline_style(body, snapshot.newline)
                    data = _encode_snapshot_text(snapshot, rendered)
                    expected = snapshot.fingerprint
                    mode = snapshot.mode
                    require_missing = False
                else:
                    if expected_fingerprint not in (None, "missing"):
                        raise AtomicWriteConflict(f"{path}: expected an existing file")
                    rendered = body
                    data = body.encode("utf-8")
                    expected = None
                    mode = None
                    require_missing = True
                if _resolve_tool_path(path) != path:
                    raise AtomicWriteConflict(f"{path}: canonical target changed during write")
                policy = _validate_file_policy(ctx, {path: data})
                fingerprint = atomic_write_bytes(
                    path,
                    data,
                    expected_fingerprint=expected,
                    require_missing=require_missing,
                    mode=mode,
                    history_context=history_context,
                )
                return (
                    old,
                    rendered,
                    len(data),
                    old_fingerprint,
                    fingerprint,
                    snapshot.encoding if snapshot is not None else "utf-8",
                    snapshot.newline if snapshot is not None else _dominant_newline(rendered),
                    policy,
                )
            (
                old_content,
                rendered_body,
                size,
                old_fingerprint,
                fingerprint,
                encoding,
                newline,
                policy,
            ) = await asyncio.to_thread(_write)
        except AtomicWriteConflict as e:
            return {"error": f"write_file: conflict: {e}. Read the file again and retry.", "exit_code": 1, "conflict": True}
        except UnicodeDecodeError:
            return {"error": f"write_file: {path}: unsupported text encoding or binary file", "exit_code": 1}
        except ProjectPolicyBlocked as e:
            return {
                "error": f"write_file: {e}",
                "exit_code": 1,
                "blocked": True,
                "policy_findings": e.findings,
            }
        except PermissionError:
            return {"error": f"write_file: {path}: permission denied", "exit_code": 1}
        except OSError as e:
            return {"error": f"write_file: {path}: {e}", "exit_code": 1}
        diff = _unified_diff(old_content, rendered_body, path)
        result = {
            "output": f"Wrote {size} bytes to {path}",
            "exit_code": 0,
            "path": path,
            "old_fingerprint": old_fingerprint,
            "fingerprint": fingerprint,
            "history": last_history_status(history_context),
        }
        if diff:
            result["diff"] = diff
        if policy.get("warnings"):
            result["policy_warnings"] = policy["warnings"]
        return _with_file_result(
            result,
            operation="write",
            path=path,
            kind="text",
            range={"unit": "byte", "start": 0, "end": size - 1} if size else None,
            page={
                "unit": "byte",
                "cursor": 0,
                "next_cursor": None,
                "has_more": False,
                "returned": size,
                "total": size,
            },
            bytes_considered=size,
            lines_considered=len(rendered_body.splitlines()),
            encoding=encoding,
            newline={"\n": "lf", "\r\n": "crlf", "\r": "cr"}[newline],
            media_type="text/plain",
            fingerprint=fingerprint,
        )

class ApplyPatchTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        """Apply a small Codex-style patch using exact context matching.

        This is deliberately stricter than git-apply: if an update hunk's old
        text is not found exactly once, the whole patch is rejected before any
        file is changed. That keeps agent edits reviewable and avoids fuzzy
        corruption when the model patches stale context.
        """
        if _agent_service_enabled(ctx):
            return {"error": "apply_patch: use edit_file/write_file while the Rust agent lane is active", "exit_code": 1, "code": "unsupported"}
        from src.tool_execution import _resolve_tool_path
        history_context = _history_context(ctx)

        patch_text = content or ""
        expected_fingerprints: Dict[str, str] = {}
        stripped = patch_text.strip()
        if stripped.startswith("{"):
            try:
                args = json.loads(stripped)
                if isinstance(args, dict):
                    patch_text = str(args.get("patch_text") or args.get("patchText") or args.get("patch") or "")
                    raw_expected = args.get("expected_fingerprints")
                    if isinstance(raw_expected, dict):
                        expected_fingerprints = {
                            str(key): str(value)
                            for key, value in raw_expected.items()
                            if value is not None
                        }
            except (json.JSONDecodeError, TypeError):
                pass
        if not patch_text.strip():
            return {"error": "apply_patch: patch_text required", "exit_code": 1}

        try:
            ops = _parse_agent_patch(patch_text)
            if not ops:
                return {"error": "apply_patch: no file operations found", "exit_code": 1}
            prepared = []
            for op in ops:
                path = _resolve_tool_path(op["path"])
                kind = op["kind"]
                if kind == "add":
                    if os.path.exists(path):
                        return {"error": f"apply_patch: {op['path']}: already exists", "exit_code": 1}
                    old = ""
                    new = op["content"]
                    change = AtomicFileChange(
                        path=path,
                        data=new.encode("utf-8"),
                        require_missing=True,
                        history_context=history_context,
                    )
                elif kind == "delete":
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    snapshot = _read_text_snapshot(path)
                    expected = expected_fingerprints.get(op["path"], expected_fingerprints.get(path))
                    _assert_expected_fingerprint(path, expected, snapshot.fingerprint)
                    old = snapshot.text
                    new = ""
                    change = AtomicFileChange(
                        path=path,
                        data=None,
                        expected_fingerprint=snapshot.fingerprint,
                        mode=snapshot.mode,
                        history_context=history_context,
                    )
                else:
                    if not os.path.isfile(path):
                        return {"error": f"apply_patch: {op['path']}: not found", "exit_code": 1}
                    snapshot = _read_text_snapshot(path)
                    expected = expected_fingerprints.get(op["path"], expected_fingerprints.get(path))
                    _assert_expected_fingerprint(path, expected, snapshot.fingerprint)
                    old = snapshot.text
                    normalized = old.replace("\r\n", "\n").replace("\r", "\n")
                    updated = _apply_patch_hunks(normalized, op["hunks"], op["path"])
                    new = _preserve_newline_style(updated, snapshot.newline)
                    change = AtomicFileChange(
                        path=path,
                        data=_encode_snapshot_text(snapshot, new),
                        expected_fingerprint=snapshot.fingerprint,
                        mode=snapshot.mode,
                        history_context=history_context,
                    )
                prepared.append((kind, path, old, new, change))

            diffs = []
            for _, path, _, _, _ in prepared:
                if _resolve_tool_path(path) != path:
                    raise AtomicWriteConflict(f"{path}: canonical target changed during patch")
            policy = await asyncio.to_thread(
                _validate_file_policy,
                ctx,
                {
                    path: None if kind == "delete" else change.data
                    for kind, path, _, _, change in prepared
                },
            )
            atomic_write_batch([change for _, _, _, _, change in prepared])
            for kind, path, old, new, _ in prepared:
                diff = _unified_diff(old, new, path)
                if diff:
                    diffs.append(diff)
        except AtomicWriteConflict as e:
            return {"error": f"apply_patch: conflict: {e}. Read the files again and retry.", "exit_code": 1, "conflict": True}
        except ProjectPolicyBlocked as e:
            return {
                "error": f"apply_patch: {e}",
                "exit_code": 1,
                "blocked": True,
                "policy_findings": e.findings,
            }
        except (ValueError, UnicodeDecodeError, PermissionError, OSError) as e:
            return {"error": f"apply_patch: {e}", "exit_code": 1}

        added = sum(int(d.get("added") or 0) for d in diffs)
        removed = sum(int(d.get("removed") or 0) for d in diffs)
        text_parts = [d.get("text", "") for d in diffs if d.get("text")]
        diff_text = "\n".join(text_parts)
        if len(diff_text.splitlines()) > MAX_DIFF_LINES:
            diff_text = "\n".join(diff_text.splitlines()[:MAX_DIFF_LINES]) + f"\n... diff truncated at {MAX_DIFF_LINES} lines"
        result = {
            "output": f"Applied patch ({len(prepared)} file{'s' if len(prepared) != 1 else ''}, +{added}/-{removed})",
            "exit_code": 0,
            "files": [
                {
                    "path": path,
                    "operation": kind,
                    "old_fingerprint": change.expected_fingerprint,
                    "fingerprint": file_fingerprint(path),
                }
                for kind, path, _, _, change in prepared
            ],
        }
        if diffs:
            result["diff"] = {
                "text": diff_text,
                "added": added,
                "removed": removed,
                "new_file": any(d.get("new_file") for d in diffs),
                "file": "patch",
            }
        if policy.get("warnings"):
            result["policy_warnings"] = policy["warnings"]
        return result

def _parse_agent_patch(patch_text: str) -> List[Dict[str, Any]]:
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise ValueError("patch must start with *** Begin Patch")
    if lines[-1].strip() != "*** End Patch":
        raise ValueError("patch must end with *** End Patch")

    ops: List[Dict[str, Any]] = []
    i = 1
    while i < len(lines) - 1:
        line = lines[i]
        if not line:
            i += 1
            continue
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: "):].strip()
            body = []
            i += 1
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if not lines[i].startswith("+"):
                    raise ValueError(f"add file {path}: every content line must start with +")
                body.append(lines[i][1:])
                i += 1
            ops.append({"kind": "add", "path": path, "content": "\n".join(body) + ("\n" if body else "")})
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: "):].strip()
            ops.append({"kind": "delete", "path": path})
            i += 1
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: "):].strip()
            hunks = []
            current = []
            i += 1
            if i < len(lines) - 1 and lines[i].startswith("*** Move to: "):
                raise ValueError("move operations are not supported")
            while i < len(lines) - 1 and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if current:
                        hunks.append(current)
                        current = []
                elif lines[i].startswith((" ", "-", "+")):
                    current.append(lines[i])
                elif lines[i] == "":
                    current.append(" ")
                else:
                    raise ValueError(f"update file {path}: invalid patch line {lines[i]!r}")
                i += 1
            if current:
                hunks.append(current)
            if not hunks:
                raise ValueError(f"update file {path}: no hunks")
            ops.append({"kind": "update", "path": path, "hunks": hunks})
            continue
        raise ValueError(f"unexpected patch line: {line!r}")
    return ops

def _apply_patch_hunks(original: str, hunks: List[List[str]], label: str) -> str:
    updated = original
    for idx, hunk in enumerate(hunks, 1):
        old_lines = []
        new_lines = []
        for line in hunk:
            prefix, body = line[:1], line[1:]
            if prefix in (" ", "-"):
                old_lines.append(body)
            if prefix in (" ", "+"):
                new_lines.append(body)
        old_text = "\n".join(old_lines)
        new_text = "\n".join(new_lines)
        if old_text and old_text in updated:
            occurrences = updated.count(old_text)
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text, new_text, 1)
        elif old_text + "\n" in updated:
            occurrences = updated.count(old_text + "\n")
            if occurrences != 1:
                raise ValueError(f"{label}: hunk {idx} context matched {occurrences} times")
            updated = updated.replace(old_text + "\n", new_text + "\n", 1)
        else:
            raise ValueError(f"{label}: hunk {idx} context not found")
    return updated

class LsTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_ls(content, ctx)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import (
            _is_control_data_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        raw_path = ""
        _s = (content or "").strip()
        args: Dict[str, Any] = {}
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
                raw_path = str(args.get("path", "")).strip()
            except json.JSONDecodeError:
                raw_path = ""
        else:
            raw_path = _s.split("\n", 1)[0].strip()
        try:
            root = _resolve_search_root(raw_path)
        except ValueError as e:
            return {"error": f"ls: {e}", "exit_code": 1}

        def _ls():
            if not os.path.isdir(root):
                return None, f"ls: {root}: not a directory", [], 0, None, 0
            rows = []
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.name.startswith("."):
                            continue
                        if _is_control_data_path(os.path.realpath(entry.path)):
                            continue
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            size = entry.stat(follow_symlinks=False).st_size if not is_dir else 0
                        except OSError:
                            continue
                        rows.append((is_dir, entry.name, size))
            except (PermissionError, OSError) as _e:
                return None, f"ls: {_e}", [], 0, None, 0
            rows.sort(key=lambda r: (not r[0], r[1].lower()))
            try:
                cursor = max(0, int(args.get("cursor") or 0))
                limit = max(1, min(int(args.get("limit") or _CODENAV_MAX_HITS), _CODENAV_MAX_HITS))
            except (TypeError, ValueError):
                cursor, limit = 0, _CODENAV_MAX_HITS
            page = rows[cursor:cursor + limit]
            next_cursor = cursor + len(page) if cursor + len(page) < len(rows) else None
            lines = [f"{root}:"]
            for is_dir, name, size in page:
                lines.append(f"  {name}/" if is_dir else f"  {name}  ({size} B)")
            if next_cursor is not None:
                lines.append(f"  ... [{len(rows) - next_cursor} more]")
            if not rows:
                lines.append("  (empty)")
            return "\n".join(lines), None, page, cursor, next_cursor, len(rows)

        out, err, rows, cursor, next_cursor, total = await asyncio.to_thread(_ls)
        if err:
            return {"error": err, "exit_code": 1}
        entries = [
            {
                "name": name,
                "path": os.path.join(root, name),
                "kind": "directory" if is_dir else "file",
                "type": "directory" if is_dir else "file",
                "size": size,
            }
            for is_dir, name, size in rows
        ]
        return _with_file_result({
            "output": _truncate(out),
            "exit_code": 0,
            "path": root,
            "entries": entries,
            "page": {
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
                "total": total,
            },
        }, operation="list", path=root, kind="directory", range={
            "unit": "entry",
            "start": cursor,
            "end": cursor + len(entries) - 1,
        } if entries else None, page={
            "unit": "entry",
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "returned": len(entries),
            "total": total,
        }, truncation_reason="result_limit" if next_cursor is not None else None,
            media_type="inode/directory", items=entries)

class GlobTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_search(content, ctx, content_search=False)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import (
            _SENSITIVE_BASENAMES,
            _is_control_data_path,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "glob: pattern is required", "exit_code": 1}
        try:
            cursor = max(0, int(args.get("cursor") or 0))
            limit = max(1, min(int(args.get("limit") or _CODENAV_MAX_HITS), _CODENAV_MAX_HITS))
        except (TypeError, ValueError):
            cursor, limit = 0, _CODENAV_MAX_HITS
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"glob: {e}", "exit_code": 1}

        def _glob():
            base = os.path.abspath(root)
            if not os.path.isdir(base):
                return None, f"glob: {root}: not a directory"
            rbase = os.path.realpath(base)
            norm_pat = pattern.replace("\\", "/")
            # Fast path: literal pattern (no wildcards) → direct path lookup.
            if not any(c in norm_pat for c in "*?["):
                cand = os.path.realpath(os.path.join(base, norm_pat))
                # Keep the literal lookup inside the search root. os.path.join
                # lets an absolute pattern (or one containing ../) escape `base`,
                # which would turn glob into an existence/path oracle for
                # arbitrary host files — bypassing the workspace/allowlist
                # confinement that _resolve_search_root applies to the root.
                # An escaping literal falls through to the walk, which only ever
                # yields paths under base.
                nbase = os.path.normcase(rbase)
                try:
                    inside = cand == rbase or os.path.commonpath(
                        [os.path.normcase(cand), nbase]
                    ) == nbase
                except ValueError:
                    inside = False
                # A literal that names a deny-listed sensitive file (.env,
                # .ssh/id_rsa, …) falls through to the walk, which skips it —
                # otherwise glob would surface secret paths that read_file /
                # grep already refuse to touch.
                if (
                    inside
                    and os.path.exists(cand)
                    and not _is_sensitive_path(cand)
                    and not _is_control_data_path(cand)
                ):
                    return [cand], None
                # Literal not at exact path — fall through to walk so
                # e.g. "foo.py" still matches at any depth (like rglob).
            # Compile glob to regex: * stays within one segment, **/ spans dirs.
            regex = _glob_to_regex(norm_pat)
            matched = []
            try:
                for dp, dns, fns in os.walk(base):
                    # Prune skipped dirs before descending (unlike rglob which
                    # descends first then filters — fatal on large node_modules).
                    # Sensitive dirs (.ssh, .gnupg, …) are pruned too so glob
                    # never enumerates the keys/tokens inside them.
                    dns[:] = [
                        d for d in dns
                        if (
                            d not in _CODENAV_SKIP_DIRS
                            and d not in _SENSITIVE_BASENAMES
                            and not _is_control_data_path(
                                os.path.realpath(os.path.join(dp, d))
                            )
                        )
                    ]
                    for name in fns + dns:
                        full = os.path.join(dp, name)
                        rel = os.path.relpath(full, base).replace(os.sep, "/")
                        if regex.fullmatch(rel) or regex.fullmatch(name):
                            # Skip deny-listed sensitive files (.env, id_rsa,
                            # known_hosts, …) the same way grep does.
                            if (
                                _is_sensitive_path(os.path.realpath(full))
                                or _is_control_data_path(os.path.realpath(full))
                            ):
                                continue
                            try:
                                mtime = os.stat(full).st_mtime
                            except OSError:
                                mtime = 0
                            matched.append((mtime, full))
            except OSError as _e:
                return None, f"glob: {_e}"
            matched.sort(
                key=lambda item: (-item[0], os.path.normcase(item[1]), item[1])
            )
            return [pth for _, pth in matched], None

        paths, err = await asyncio.to_thread(_glob)
        if err:
            return {"error": err, "exit_code": 1}
        if not paths:
            return _with_file_result({
                "output": f"No files matching {pattern!r} under {root}",
                "exit_code": 0,
                "path": root,
                "paths": [],
                "page": {
                    "cursor": cursor,
                    "next_cursor": None,
                    "has_more": False,
                    "total": 0,
                },
            }, operation="glob", path=root, kind="search", page={
                "unit": "result",
                "cursor": cursor,
                "next_cursor": None,
                "has_more": False,
                "returned": 0,
                "total": 0,
            }, items=[])
        page = paths[cursor:cursor + limit]
        next_cursor = cursor + len(page) if len(paths) > cursor + len(page) else None
        out = "\n".join(page)
        if next_cursor is not None:
            out += f"\n... [more files; continue at cursor {next_cursor}]"
        items = [{"path": path, "kind": "path"} for path in page]
        return _with_file_result({
            "output": _truncate(out or f"No files on page starting at cursor {cursor}"),
            "exit_code": 0,
            "path": root,
            "paths": page,
            "page": {
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
                "result_count": len(page),
                "total": len(paths),
            },
        }, operation="glob", path=root, kind="search", range={
            "unit": "result",
            "start": cursor,
            "end": cursor + len(page) - 1,
        } if page else None, page={
            "unit": "result",
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "returned": len(page),
            "total": len(paths),
        }, truncation_reason="result_limit" if next_cursor is not None else None,
            items=items)

class GrepTool:
    async def execute(self, content: str, ctx: dict) -> dict:
        agent_result = await _agent_search(content, ctx, content_search=True)
        if agent_result is not None:
            return agent_result
        from src.tool_execution import (
            _control_data_roots,
            _SENSITIVE_FILE_PATTERNS,
            _is_control_data_path,
            _is_sensitive_path,
            _resolve_tool_path,
            _resolve_search_root,
            _truncate,
        )
        args: Dict[str, Any] = {}
        _s = (content or "").strip()
        if _s.startswith("{"):
            try:
                args = json.loads(_s)
            except json.JSONDecodeError:
                args = {}
        else:
            args = {"pattern": _s}
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"error": "grep: pattern is required", "exit_code": 1}
        ignore_case = bool(args.get("ignore_case"))
        literal = bool(args.get("literal")) or str(args.get("mode") or "").lower() in {
            "literal",
            "fixed",
        }
        glob_pat = str(args.get("glob", "") or "").strip()
        try:
            cursor = max(0, int(args.get("cursor") or 0))
            limit = max(
                1,
                min(
                    int(args.get("limit") or args.get("max_results") or _CODENAV_MAX_HITS),
                    _CODENAV_MAX_HITS,
                ),
            )
        except (TypeError, ValueError):
            cursor, limit = 0, _CODENAV_MAX_HITS
        scan_cap = 10_000
        try:
            root = _resolve_search_root(str(args.get("path", "")))
        except ValueError as e:
            return {"error": f"grep: {e}", "exit_code": 1}

        def _grep():
            import re as _re
            import shutil
            rg = shutil.which("rg")
            if rg:
                cmd = [rg, "--line-number", "--no-heading", "--color=never",
                       "--max-count", str(scan_cap)]
                if ignore_case:
                    cmd.append("--ignore-case")
                if literal:
                    cmd.append("--fixed-strings")
                if glob_pat:
                    cmd += ["--glob", glob_pat]
                # --iglob (not --glob) so the exclusion is case-insensitive:
                # on a case-insensitive filesystem "ID_RSA"/"Known_Hosts"
                # resolve to the same secret as their lowercase forms, and the
                # Python fallback below already folds case via _is_sensitive_path.
                for _pat in _SENSITIVE_FILE_PATTERNS:
                    cmd += ["--iglob", f"!*{_pat}*"]
                for _d in _CODENAV_SKIP_DIRS:
                    cmd += ["--glob", f"!**/{_d}/**"]
                for protected in _control_data_roots():
                    try:
                        relative = os.path.relpath(protected, root)
                    except ValueError:
                        continue
                    if relative == ".." or relative.startswith(f"..{os.sep}"):
                        continue
                    relative = relative.replace(os.sep, "/")
                    cmd += ["--glob", f"!{relative}", "--glob", f"!{relative}/**"]
                cmd += ["--regexp", pattern, root]
                try:
                    import subprocess
                    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
                    lines = [ln for ln in (p.stdout or "").splitlines() if ln][:scan_cap]
                    return lines, None
                except subprocess.TimeoutExpired:
                    return None, "grep: timed out"
                except Exception as _e:
                    return None, f"grep: {_e}"
            try:
                rx = _re.compile(
                    _re.escape(pattern) if literal else pattern,
                    _re.IGNORECASE if ignore_case else 0,
                )
            except _re.error as _e:
                return None, f"grep: bad pattern: {_e}"
            hits = []
            if os.path.isfile(root):
                file_iter = [root]
            else:
                file_iter = []
                for dp, dns, fns in os.walk(root):
                    dns[:] = [
                        d for d in dns
                        if (
                            d not in _CODENAV_SKIP_DIRS
                            and not _is_control_data_path(
                                os.path.realpath(os.path.join(dp, d))
                            )
                        )
                    ]
                    for fn in fns:
                        if glob_pat and not fnmatch.fnmatch(fn, glob_pat):
                            continue
                        file_iter.append(os.path.join(dp, fn))
            for fp in file_iter:
                if len(hits) >= scan_cap:
                    break
                if (
                    _is_sensitive_path(os.path.realpath(fp))
                    or _is_control_data_path(os.path.realpath(fp))
                ):
                    continue
                try:
                    with open(fp, "r", encoding="utf-8", errors="strict") as f:
                        for i, line in enumerate(f, 1):
                            if rx.search(line):
                                hits.append(f"{fp}:{i}:{line.rstrip()[:_CODENAV_MAX_LINE]}")
                                if len(hits) >= scan_cap:
                                    break
                except (UnicodeDecodeError, OSError):
                    continue
            return hits, None

        lines, err = await asyncio.to_thread(_grep)
        if err:
            return {"error": err, "exit_code": 1}
        def _grep_sort_key(line: str):
            match = re.match(r"^(.*?):(\d+):(.*)$", line)
            path = match.group(1) if match else ""
            line_number = int(match.group(2)) if match else 0
            text = match.group(3) if match else line
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                mtime = 0
            return (-mtime, os.path.normcase(path), path, line_number, text)

        lines.sort(key=_grep_sort_key)
        if not lines:
            return _with_file_result({
                "output": f"No matches for {pattern!r} under {root}",
                "exit_code": 0,
                "path": root,
                "matches": [],
                "search_mode": "literal" if literal else "regex",
                "page": {
                    "cursor": cursor,
                    "next_cursor": None,
                    "has_more": False,
                    "result_count": 0,
                },
            }, operation="grep", path=root, kind="search", page={
                "unit": "result",
                "cursor": cursor,
                "next_cursor": None,
                "has_more": False,
                "returned": 0,
                "total": 0,
            }, search_mode="literal" if literal else "regex", items=[])
        page = lines[cursor:cursor + limit]
        next_cursor = cursor + len(page) if len(lines) > cursor + len(page) else None
        out = "\n".join(ln[:_CODENAV_MAX_LINE] for ln in page)
        if next_cursor is not None:
            out += f"\n... [more matches; continue at cursor {next_cursor}]"

        def _typed_match(line: str) -> Dict[str, Any]:
            match = re.match(r"^(.*?):(\d+):(.*)$", line)
            if not match:
                return {"path": "", "line": None, "text": line[:_CODENAV_MAX_LINE]}
            return {
                "path": match.group(1),
                "line": int(match.group(2)),
                "text": match.group(3)[:_CODENAV_MAX_LINE],
            }

        matches = [_typed_match(line) for line in page]
        return _with_file_result({
            "output": _truncate(out or f"No matches on page starting at cursor {cursor}"),
            "exit_code": 0,
            "path": root,
            "matches": matches,
            "search_mode": "literal" if literal else "regex",
            "page": {
                "cursor": cursor,
                "next_cursor": next_cursor,
                "has_more": next_cursor is not None,
                "result_count": len(page),
            },
        }, operation="grep", path=root, kind="search", range={
            "unit": "result",
            "start": cursor,
            "end": cursor + len(page) - 1,
        } if page else None, page={
            "unit": "result",
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor is not None,
            "returned": len(page),
            "total": None if next_cursor is not None else cursor + len(page),
        }, truncation_reason="result_limit" if next_cursor is not None else None,
            search_mode="literal" if literal else "regex", items=matches)

class GetWorkspaceTool:
    """Report the active workspace folder (no args). File tools are confined to
    it; the shell starts there (cwd) but is NOT sandboxed."""
    async def execute(self, content: str, ctx: dict) -> dict:
        from src.tool_execution import get_active_workspace
        ws = get_active_workspace()
        if ws:
            return {
                "output": f"{ws}\n(File tools are confined to this folder; the shell starts "
                          f"here but is not sandboxed and can reach outside it.)",
                "exit_code": 0,
            }
        return {
            "output": "No workspace is set. File tools use the default allowed roots; "
                      "resolve paths from the user or use absolute paths.",
            "exit_code": 0,
        }

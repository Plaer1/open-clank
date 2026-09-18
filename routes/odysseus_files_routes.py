"""Authenticated app-principal adapter for the Rust filesystem service."""

from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import secrets
import threading
import time
from collections import OrderedDict
from urllib.parse import quote
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse

from src.auth_helpers import get_current_user
from src.openclank.files_service_client import FilesServiceError, client_for_owner, close_all_clients
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry
from src.tool_security import owner_is_admin_or_single_user


_PREVIEW_HANDLE_TTL_SECONDS = 5 * 60
_PREVIEW_HANDLES_PER_OWNER = 16
_PREVIEW_HANDLE_GLOBAL_LIMIT = 512
_PREVIEW_MAGIC_BYTES = 512
_TEXT_PREVIEW_MAX_BYTES = 320_000


def project_navigation_roots(
    registry: FilesystemRootRegistry,
    owner: str,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """Project app-visible host anchors without probing unassigned resources.

    This is shared by the compatibility Files route and the canonical Host
    provider. It is metadata projection only; every child/stat/content request
    still enters the Rust service with the same server-minted app scope.
    """
    home = os.path.realpath(os.path.expanduser("~"))
    roots: list[dict[str, Any]] = []
    favorites: list[dict[str, Any]] = []

    if scope.get("host"):
        if os.name == "nt":
            list_drives = getattr(os, "listdrives", None)
            drives = list_drives() if callable(list_drives) else [
                f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{letter}:\\")
            ]
            for drive in drives:
                canonical = os.path.realpath(str(drive))
                roots.append({
                    "id": f"host:{canonical}",
                    "name": canonical,
                    "path": canonical,
                    "kind": "recursive_directory",
                    "capabilities": ["read", "write"],
                })
        else:
            roots.append({
                "id": "host:/",
                "name": "/",
                "path": os.path.realpath(os.path.sep),
                "kind": "recursive_directory",
                "capabilities": ["read", "write"],
            })
        if os.path.isdir(home):
            favorites.append({
                "id": "home",
                "name": "Home",
                "path": home,
                "kind": "recursive_directory",
                "pinned": True,
            })
        return {
            "version": 1,
            "generation": int(scope.get("generation") or 0),
            "default_path": home if os.path.isdir(home) else (roots[0]["path"] if roots else ""),
            "roots": roots,
            "favorites": favorites,
        }

    assignments = registry.visibility_for_subject(owner)
    seen: set[str] = set()
    for assignment in assignments:
        root = assignment.get("root") or {}
        assignment_caps = set(str(value) for value in assignment.get("capabilities") or [])
        root_caps = set(str(value) for value in root.get("capabilities") or [])
        if "read" not in assignment_caps or "read" not in root_caps:
            continue
        if not root.get("enabled") or root.get("availability") != "available":
            continue
        canonical = str(root.get("canonical_path") or "")
        kind = str(root.get("kind") or "")
        if not canonical or canonical in seen or kind not in {"recursive_directory", "exact_file"}:
            continue
        seen.add(canonical)
        roots.append({
            "id": str(root.get("id") or assignment.get("root_id") or f"assigned:{len(roots)}"),
            "name": os.path.basename(canonical.rstrip(os.sep)) or canonical,
            "path": canonical,
            "kind": kind,
            "capabilities": sorted(assignment_caps & root_caps),
        })
    roots.sort(key=lambda item: (item["kind"] == "exact_file", item["name"].casefold(), item["path"].casefold()))
    default = next((item["path"] for item in roots if item["kind"] == "recursive_directory"), "")
    if default:
        favorites.append({
            "id": "assigned-start",
            "name": next(item["name"] for item in roots if item["path"] == default),
            "path": default,
            "kind": "recursive_directory",
            "pinned": True,
        })
    return {
        "version": 1,
        "generation": int(scope.get("generation") or 0),
        "default_path": default,
        "roots": roots,
        "favorites": favorites,
    }


def _fingerprint_value(metadata: dict[str, Any]) -> str:
    fingerprint = metadata.get("fingerprint")
    if isinstance(fingerprint, dict):
        return str(fingerprint.get("value") or "")
    return str(fingerprint or "")


def _cheap_file_identity(metadata: dict[str, Any]) -> tuple[int, int | None]:
    """Return size + modified time without triggering another content hash."""
    try:
        size = int(metadata.get("size") or 0)
    except (TypeError, ValueError):
        size = -1
    modified = metadata.get("modified_unix_ms")
    try:
        modified_ms = int(modified) if modified is not None else None
    except (TypeError, ValueError):
        modified_ms = None
    return size, modified_ms


def _detected_preview_type(header: bytes, requested_kind: str, filename: str) -> tuple[str, str] | None:
    """Return a script-inert browser media type from bounded magic bytes.

    Extensions only disambiguate otherwise safe audio containers.  In
    particular, SVG/XML/HTML are never image preview types: those formats can
    execute or navigate when embedded and belong in the bounded text viewer.
    """
    kind = str(requested_kind or "").strip().lower()
    suffix = os.path.splitext(str(filename or ""))[1].lower()
    if kind == "image":
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image", "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image", "image/jpeg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return "image", "image/gif"
        if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
            return "image", "image/webp"
        if header.startswith(b"BM"):
            return "image", "image/bmp"
        if header.startswith((b"II*\x00", b"MM\x00*")):
            return "image", "image/tiff"
        if header.startswith(b"\x00\x00\x01\x00"):
            return "image", "image/x-icon"
        if len(header) >= 12 and header[4:8] == b"ftyp" and header[8:12] in {b"avif", b"avis"}:
            return "image", "image/avif"
        return None
    if kind != "audio":
        return None
    if header.startswith(b"fLaC"):
        return "audio", "audio/flac"
    if header.startswith(b"OggS"):
        return "audio", "audio/ogg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return "audio", "audio/wav"
    if len(header) >= 12 and header[:4] == b"FORM" and header[8:12] in {b"AIFF", b"AIFC"}:
        return "audio", "audio/aiff"
    if header.startswith(b"MThd"):
        return "audio", "audio/midi"
    if header.startswith(b"ID3"):
        return "audio", "audio/mpeg"
    if len(header) >= 4 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return "audio", "audio/aac" if suffix in {".aac", ".adts"} else "audio/mpeg"
    if len(header) >= 12 and header[4:8] == b"ftyp" and suffix in {".m4a", ".m4b", ".mp4"}:
        return "audio", "audio/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3") and suffix in {".webm", ".weba"}:
        return "audio", "audio/webm"
    return None


class _PreviewHandleStore:
    """Small process-local capability table for host media previews.

    The process-local design intentionally fails closed under a multi-worker
    deployment: a handle routed to another worker receives the same typed
    ``preview_handle_stale`` response as an expired handle.  A shared,
    authenticated capability store is required before enabling sticky-free
    multi-worker preview traffic.
    """

    def __init__(self) -> None:
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._owners: dict[str, OrderedDict[str, None]] = {}
        self._lock = threading.RLock()

    def _remove_locked(self, token: str) -> None:
        record = self._records.pop(token, None)
        if not record:
            return
        owner_tokens = self._owners.get(str(record["owner"]))
        if owner_tokens is not None:
            owner_tokens.pop(token, None)
            if not owner_tokens:
                self._owners.pop(str(record["owner"]), None)

    def _prune_locked(self, now: float) -> None:
        for token, record in list(self._records.items()):
            if float(record.get("expires_monotonic") or 0) <= now:
                self._remove_locked(token)

    def mint(self, owner: str, record: dict[str, Any]) -> tuple[str, float]:
        now = time.monotonic()
        expires_monotonic = now + _PREVIEW_HANDLE_TTL_SECONDS
        with self._lock:
            self._prune_locked(now)
            owner_tokens = self._owners.setdefault(owner, OrderedDict())
            while len(owner_tokens) >= _PREVIEW_HANDLES_PER_OWNER:
                oldest = next(iter(owner_tokens))
                self._remove_locked(oldest)
                owner_tokens = self._owners.setdefault(owner, OrderedDict())
            while len(self._records) >= _PREVIEW_HANDLE_GLOBAL_LIMIT:
                self._remove_locked(next(iter(self._records)))
            token = secrets.token_urlsafe(32)
            while token in self._records:
                token = secrets.token_urlsafe(32)
            stored = dict(record)
            stored.update({"owner": owner, "expires_monotonic": expires_monotonic})
            self._records[token] = stored
            owner_tokens[token] = None
        return token, time.time() + _PREVIEW_HANDLE_TTL_SECONDS

    def get(self, token: str, owner: str, session: str) -> dict[str, Any] | None:
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(token)
            if not record or not secrets.compare_digest(str(record.get("owner") or ""), owner) or not secrets.compare_digest(str(record.get("session") or ""), session):
                return None
            self._records.move_to_end(token)
            owner_tokens = self._owners.get(owner)
            if owner_tokens is not None and token in owner_tokens:
                owner_tokens.move_to_end(token)
            return dict(record)

    def revoke(self, token: str, owner: str, session: str | None = None) -> None:
        with self._lock:
            record = self._records.get(token)
            session_matches = session is None or secrets.compare_digest(str(record.get("session") or "") if record else "", session)
            if record and str(record.get("owner")) == owner and session_matches:
                self._remove_locked(token)


def setup_odysseus_files_routes() -> APIRouter:
    router = APIRouter(prefix="/api/odysseus-files", tags=["odysseus-files"])
    registry = FilesystemRootRegistry()
    preview_handles = _PreviewHandleStore()

    def authenticated_owner(request: Request) -> str:
        owner = get_current_user(request)
        if not owner:
            raise HTTPException(status_code=401, detail="Authentication required")
        return str(owner)

    def authenticated_session(request: Request) -> str:
        """Return a non-reversible binding for the authenticated browser session."""
        token = str(request.cookies.get("odysseus_session") or "")
        if not token:
            raise HTTPException(status_code=401, detail="A browser session is required for media preview")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def require_admin(request: Request) -> str:
        owner = authenticated_owner(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(status_code=403, detail="Administrator permission required")
        return owner

    def assigned_root_allows(owner: str, path: str, kind: str, capabilities: list[str]) -> bool:
        """Check a non-admin agent-root mutation against its app ceiling."""
        target = os.path.realpath(os.path.expanduser(str(path or "").strip()))
        requested = set(str(value) for value in capabilities)
        try:
            assignments = registry.visibility_for_subject(owner)
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error
        for assignment in assignments:
            ceiling = assignment.get("root") or {}
            if not ceiling.get("enabled") or ceiling.get("availability") != "available":
                continue
            ceiling_path = str(ceiling.get("canonical_path") or "")
            ceiling_caps = set(str(value) for value in assignment.get("capabilities") or [])
            if not requested.issubset(ceiling_caps):
                continue
            if ceiling.get("kind") == "recursive_directory":
                try:
                    if os.path.commonpath([ceiling_path, target]) == ceiling_path:
                        return True
                except ValueError:
                    pass
            if ceiling.get("kind") == "exact_file" and kind == "exact_file" and target == ceiling_path:
                return True
        return False

    def app_context(request: Request) -> tuple[str, dict[str, Any]]:
        """Resolve the authenticated app scope for every file operation.

        Administrators retain the legacy host-wide app lane. Standard users
        receive only the roots an administrator assigned to their identity;
        an empty assignment set is still passed to Rust and therefore fails
        closed instead of silently becoming host-wide.
        """
        owner = authenticated_owner(request)
        try:
            scope = registry.app_scope(owner, is_admin=owner_is_admin_or_single_user(owner))
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error
        return owner, scope

    def app_client(request: Request):
        owner, scope = app_context(request)
        return owner, service_for_scope(owner, scope)

    def service_for_scope(owner: str, scope: dict[str, Any]):
        # Preserve the established admin client call shape for integrations
        # that monkeypatch or wrap the host-wide lane. Non-admin requests must
        # always carry the server-minted scope explicitly.
        if scope.get("host"):
            return client_for_owner(owner)
        return client_for_owner(owner, app_scope=scope)

    def service_error(error: FilesServiceError) -> HTTPException:
        status = {
            "denied": 403,
            "malformed_request": 400,
            "backpressure": 429,
            "conflict": 409,
            "stale_cursor": 409,
            "policy_generation_changed": 409,
            "root_unavailable": 503,
        }.get(error.code, 503)
        return HTTPException(status_code=status, detail={"code": error.code, "message": str(error)})

    @router.get("/browse")
    async def browse(
        request: Request,
        path: str = Query(default=""),
        cursor: str | None = Query(default=None),
        sort: str | None = Query(default=None),
    ) -> dict[str, Any]:
        owner, service = app_client(request)
        target = os.path.expanduser(path.strip() or "~")
        payload: dict[str, Any] = {}
        if cursor:
            try:
                payload["cursor"] = json.loads(cursor)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=400, detail="cursor must be JSON") from error
        if sort:
            try:
                parsed_sort = json.loads(sort)
            except json.JSONDecodeError as error:
                raise HTTPException(status_code=400, detail="sort must be JSON") from error
            if not isinstance(parsed_sort, dict):
                raise HTTPException(status_code=400, detail="sort must be an object")
            payload["sort"] = parsed_sort
        try:
            response = await service.request("list_directory", target, payload)
        except FilesServiceError as error:
            raise service_error(error) from error
        return response

    @router.get("/read")
    async def read(
        request: Request,
        path: str = Query(...),
        offset: int = Query(default=0, ge=0),
        length: int = Query(default=65536, ge=1, le=8 * 1024 * 1024),
        fingerprint: bool = Query(default=False),
    ) -> dict[str, Any]:
        owner, service = app_client(request)
        try:
            return await service.request(
                "read_range",
                path,
                {"offset": offset, "length": length, "include_fingerprint": fingerprint},
            )
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.get("/read-text")
    async def read_text(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Decode text in Rust so BOM/newline/encoding behavior stays shared."""
        _, service = app_client(request)
        try:
            return await service.request("read_lines", path, {})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.get("/preview-text")
    async def preview_text(request: Request, path: str = Query(...)) -> dict[str, Any]:
        """Return Rust-decoded head/tail text under one fixed byte budget."""
        _, service = app_client(request)
        try:
            return await service.request(
                "read_text_preview",
                path,
                {"max_bytes": _TEXT_PREVIEW_MAX_BYTES},
            )
        except FilesServiceError as error:
            if error.code == "invalid_path":
                raise HTTPException(
                    status_code=415,
                    detail={"code": "unsupported_text_preview", "message": "The file is not supported as text"},
                ) from error
            raise service_error(error) from error

    def preview_error(status: int, code: str, message: str) -> HTTPException:
        return HTTPException(status_code=status, detail={"code": code, "message": message})

    async def authorized_preview_record(request: Request, token: str):
        """Resolve a capability without ever reflecting its backing path."""
        owner, scope = app_context(request)
        session = authenticated_session(request)
        record = preview_handles.get(token, owner, session)
        if record is None:
            raise preview_error(410, "preview_handle_stale", "Preview handle is unavailable or expired")
        if int(scope.get("generation") or 0) != int(record.get("generation") or 0):
            preview_handles.revoke(token, owner, session)
            raise preview_error(409, "preview_policy_changed", "File visibility changed; open the preview again")
        service = service_for_scope(owner, scope)
        try:
            response = await service.request("stat", record["path"], {"include_fingerprint": False})
        except FilesServiceError as error:
            preview_handles.revoke(token, owner, session)
            if error.code == "policy_generation_changed":
                raise preview_error(409, "preview_policy_changed", "File visibility changed; open the preview again") from error
            status = 403 if error.code == "denied" else 503
            code = "preview_access_revoked" if status == 403 else "preview_unavailable"
            raise preview_error(status, code, "Preview access is no longer available") from error
        metadata = response.get("data") or {}
        if (
            str(metadata.get("kind") or "").lower() not in {"file", "regular_file"}
            or _cheap_file_identity(metadata)
            != (int(record["size"]), record.get("modified_unix_ms"))
        ):
            preview_handles.revoke(token, owner, session)
            raise preview_error(409, "preview_file_changed", "The file changed; open the preview again")
        return owner, scope, service, record, _cheap_file_identity(metadata)

    @router.post("/preview-handles")
    async def mint_preview_handle(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Mint a short-lived media capability after Rust app authorization."""
        target = str(body.get("path") or "").strip()
        requested_kind = str(body.get("kind") or "").strip().lower()
        if not target:
            raise preview_error(400, "invalid_preview_request", "A file is required")
        if requested_kind not in {"image", "audio"}:
            raise preview_error(400, "invalid_preview_kind", "Preview kind must be image or audio")
        owner, scope = app_context(request)
        session = authenticated_session(request)
        service = service_for_scope(owner, scope)
        try:
            stat_response = await service.request("stat", target, {"include_fingerprint": True})
        except FilesServiceError as error:
            status = 403 if error.code == "denied" else 503
            code = "preview_denied" if status == 403 else "preview_unavailable"
            raise preview_error(status, code, "The file is not available for preview") from error
        metadata = stat_response.get("data") or {}
        if str(metadata.get("kind") or "").lower() not in {"file", "regular_file"}:
            raise preview_error(400, "invalid_preview_target", "Preview requires a regular file")
        try:
            size = int(metadata.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        fingerprint = _fingerprint_value(metadata)
        if size <= 0 or not fingerprint:
            raise preview_error(415, "unsupported_preview_type", "The file is not a supported media preview")
        canonical_target = str(metadata.get("path") or target)
        try:
            probe_response = await service.request(
                "read_range",
                canonical_target,
                {
                    "offset": 0,
                    "length": min(size, _PREVIEW_MAGIC_BYTES),
                    "include_fingerprint": False,
                },
            )
        except FilesServiceError as error:
            status = 403 if error.code == "denied" else 503
            code = "preview_denied" if status == 403 else "preview_unavailable"
            raise preview_error(status, code, "The file is not available for preview") from error
        probe = probe_response.get("data") or {}
        try:
            header = bytes(probe.get("bytes") or [])[:_PREVIEW_MAGIC_BYTES]
        except (TypeError, ValueError):
            header = b""
        detected = _detected_preview_type(header, requested_kind, canonical_target)
        if detected is None:
            raise preview_error(415, "unsupported_preview_type", "The file is not a supported media preview")
        kind, media_type = detected
        token, expires_at = preview_handles.mint(owner, {
            "session": session,
            "path": canonical_target,
            "generation": int(scope.get("generation") or 0),
            "fingerprint": fingerprint,
            "size": size,
            "modified_unix_ms": _cheap_file_identity(metadata)[1],
            "kind": kind,
            "media_type": media_type,
        })
        return {
            "token": token,
            "url": f"/api/odysseus-files/preview/{token}",
            "kind": kind,
            "media_type": media_type,
            "size": size,
            "expires_at": expires_at,
        }

    @router.delete("/preview-handles/{token}")
    async def revoke_preview_handle(request: Request, token: str) -> dict[str, bool]:
        owner = authenticated_owner(request)
        session = authenticated_session(request)
        # Existence-blind revocation: another owner cannot discover whether a
        # token is live, and cannot revoke it either.
        preview_handles.revoke(str(token), owner, session)
        return {"ok": True}

    @router.api_route("/preview/{token}", methods=["GET", "HEAD"])
    async def preview_content(request: Request, token: str):
        """Stream media through an owner-bound opaque capability.

        No backing path is accepted, returned, placed in a header, or included
        in a route error.  Unknown/expired handles also cover the expected
        process-local miss when multiple workers are used without stickiness.
        """
        token = str(token or "")
        if len(token) < 32 or len(token) > 128:
            # Authenticate before the typed existence-blind miss.
            authenticated_owner(request)
            authenticated_session(request)
            raise preview_error(410, "preview_handle_stale", "Preview handle is unavailable or expired")
        owner, _scope, service, record, starting_identity = await authorized_preview_record(request, token)
        expected_size = int(record["size"])
        # The opaque handle itself remains bearer-like and should not be
        # echoed in metadata beyond the request URL.  The fingerprint is also
        # server-private, so derive a stable representation-specific validator.
        etag_value = hashlib.sha256(f"{token}:{record['fingerprint']}".encode("utf-8")).hexdigest()
        etag = f'"preview-{etag_value}"'
        start, end = 0, expected_size - 1
        partial = False
        range_header = str(request.headers.get("range") or "").strip()
        if range_header:
            if not range_header.lower().startswith("bytes=") or "," in range_header:
                raise HTTPException(status_code=416, detail="only one byte range is supported", headers={"Content-Range": f"bytes */{expected_size}"})
            if_range = str(request.headers.get("if-range") or "").strip()
            if if_range and if_range != etag:
                range_header = ""
            else:
                spec = range_header[6:].strip()
                try:
                    raw_start, raw_end = (spec.split("-", 1) + [""])[:2]
                    if raw_start:
                        start = int(raw_start)
                        end = int(raw_end) if raw_end else expected_size - 1
                    else:
                        suffix = int(raw_end)
                        if suffix <= 0:
                            raise ValueError
                        start = max(expected_size - suffix, 0)
                        end = expected_size - 1
                except (TypeError, ValueError):
                    raise HTTPException(status_code=416, detail="invalid byte range", headers={"Content-Range": f"bytes */{expected_size}"})
                if start < 0 or start >= expected_size or end < start:
                    raise HTTPException(status_code=416, detail="byte range is outside the file", headers={"Content-Range": f"bytes */{expected_size}"})
                end = min(end, expected_size - 1)
                partial = True

        async def body_stream():
            offset = start
            chunk_size = 1024 * 1024
            while offset <= end:
                # Administrators use a host-wide Rust lane, so explicitly keep
                # their capability bound to the same policy generation too.
                if registry.generation() != int(record["generation"]):
                    preview_handles.revoke(token, owner, str(record["session"]))
                    raise RuntimeError("preview policy changed during stream")
                try:
                    response = await service.request(
                        "read_range",
                        record["path"],
                        {
                            "offset": offset,
                            "length": min(chunk_size, end - offset + 1),
                            # The authorized stat above already recomputed and
                            # matched the complete fingerprint. Hashing the
                            # whole file again for every 1 MiB page turns a
                            # large media stream into O(n²) disk work.
                            "include_fingerprint": False,
                        },
                    )
                except FilesServiceError as error:
                    preview_handles.revoke(token, owner, str(record["session"]))
                    raise RuntimeError("preview authorization changed during stream") from error
                page = response.get("data") or {}
                try:
                    chunk = bytes(page.get("bytes") or [])[:end - offset + 1]
                except (TypeError, ValueError) as error:
                    preview_handles.revoke(token, owner, str(record["session"]))
                    raise RuntimeError("preview service returned invalid bytes") from error
                if chunk:
                    yield chunk
                next_offset = page.get("next_offset")
                if page.get("eof") or next_offset is None or int(next_offset) <= offset or not chunk:
                    break
                offset = min(int(next_offset), end + 1)
            # Reauthorize and cheaply compare identity after the final page.
            # This bounds every response with a full fingerprint before bytes
            # and a metadata identity check after bytes, without rehashing for
            # each page. A truly race-free midstream identity guarantee needs
            # a Rust descriptor-bound stream/handle (the future Tonic seam).
            try:
                final_response = await service.request("stat", record["path"], {"include_fingerprint": False})
            except FilesServiceError as error:
                preview_handles.revoke(token, owner, str(record["session"]))
                raise RuntimeError("preview authorization changed during stream") from error
            final_metadata = final_response.get("data") or {}
            if (
                str(final_metadata.get("kind") or "").lower() not in {"file", "regular_file"}
                or _cheap_file_identity(final_metadata) != starting_identity
            ):
                preview_handles.revoke(token, owner, str(record["session"]))
                raise RuntimeError("preview file changed during stream")

        headers = {
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Cache-Control": "private, no-store",
            "ETag": etag,
        }
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{expected_size}"
        status_code = 206 if partial else 200
        if request.method == "HEAD":
            return Response(content=b"", media_type=record["media_type"], headers=headers, status_code=status_code)
        return StreamingResponse(body_stream(), media_type=record["media_type"], headers=headers, status_code=status_code)

    @router.api_route("/download", methods=["GET", "HEAD"])
    async def download(request: Request, path: str = Query(...)):
        """Stream one authorized host resource to the viewing device.

        The route never opens a user path itself. Rust performs the app-scope
        check for the metadata request and every bounded read page. Downloads
        are always attachments; script-capable host content must never execute
        in the authenticated Open Clank origin. A descriptor-bound Rust stream
        remains the stronger future replacement-race boundary.
        """
        target = str(path or "").strip()
        if not target:
            raise HTTPException(status_code=400, detail="path is required")
        owner, scope = app_context(request)
        service = service_for_scope(owner, scope)
        try:
            stat_response = await service.request("stat", target, {"include_fingerprint": True})
        except FilesServiceError as error:
            raise service_error(error) from error
        metadata = stat_response.get("data") or {}
        if str(metadata.get("kind") or "").lower() not in {"file", "regular_file"}:
            raise HTTPException(status_code=400, detail="download requires a regular file")
        expected_size = int(metadata.get("size") or 0)
        expected_fingerprint = (metadata.get("fingerprint") or {}).get("value") if isinstance(metadata.get("fingerprint"), dict) else metadata.get("fingerprint")
        canonical_target = str(metadata.get("path") or target)
        starting_identity = _cheap_file_identity(metadata)
        policy_generation = int(scope.get("generation") or 0)
        raw_name = os.path.basename(canonical_target.replace("\\", "/")) or "download"
        filename = "".join(char for char in raw_name if char not in {"\r", "\n", "/", "\\", '"'})[:240] or "download"
        ascii_name = "".join(
            char for char in filename.encode("ascii", "ignore").decode("ascii")
            if char.isalnum() or char in {".", "_", "-", " "}
        ).strip() or "download"
        media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        if media_type.lower() in {
            "application/pdf",
            "application/xhtml+xml",
            "application/xml",
            "image/svg+xml",
            "text/html",
            "text/xml",
        }:
            media_type = "application/octet-stream"
        etag = f'"{expected_fingerprint}"' if expected_fingerprint else None
        start, end = 0, max(expected_size - 1, 0)
        partial = False
        range_header = str(request.headers.get("range") or "").strip()
        if range_header:
            # A single byte range keeps the Rust page contract bounded.  The
            # browser can issue another request for additional ranges.
            if not range_header.lower().startswith("bytes=") or "," in range_header:
                raise HTTPException(status_code=416, detail="only one byte range is supported")
            if_range = str(request.headers.get("if-range") or "").strip()
            if if_range and etag and if_range != etag:
                range_header = ""
            else:
                spec = range_header[6:].strip()
                try:
                    raw_start, raw_end = (spec.split("-", 1) + [""])[:2]
                    if raw_start:
                        start = int(raw_start)
                        end = int(raw_end) if raw_end else expected_size - 1
                    else:
                        suffix = int(raw_end)
                        if suffix <= 0:
                            raise ValueError
                        start = max(expected_size - suffix, 0)
                        end = expected_size - 1
                except (TypeError, ValueError):
                    raise HTTPException(status_code=416, detail="invalid byte range", headers={"Content-Range": f"bytes */{expected_size}"})
                if start < 0 or start >= expected_size or end < start:
                    raise HTTPException(status_code=416, detail="byte range is outside the file", headers={"Content-Range": f"bytes */{expected_size}"})
                end = min(end, expected_size - 1)
                partial = True

        async def body():
            offset = start
            chunk_size = 1024 * 1024
            while offset <= end and (expected_size or offset == 0):
                if registry.generation() != policy_generation:
                    raise RuntimeError("download policy changed during stream")
                try:
                    response = await service.request(
                        "read_range",
                        canonical_target,
                        {
                            "offset": offset,
                            "length": min(chunk_size, end - offset + 1) if expected_size else 1,
                            "include_fingerprint": False,
                        },
                    )
                except FilesServiceError as error:
                    raise RuntimeError(str(error)) from error
                page = response.get("data") or {}
                chunk = bytes(page.get("bytes") or [])
                if chunk:
                    yield chunk
                next_offset = page.get("next_offset")
                if page.get("eof") or next_offset is None or next_offset <= offset:
                    break
                offset = min(int(next_offset), end + 1)
            try:
                final_response = await service.request(
                    "stat",
                    canonical_target,
                    {"include_fingerprint": False},
                )
            except FilesServiceError as error:
                raise RuntimeError("download authorization changed during stream") from error
            final_metadata = final_response.get("data") or {}
            if (
                str(final_metadata.get("kind") or "").lower()
                not in {"file", "regular_file"}
                or _cheap_file_identity(final_metadata) != starting_identity
            ):
                raise RuntimeError("file changed during download")

        headers = {
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}",
            "Content-Length": str((end - start + 1) if expected_size else 0),
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        if etag:
            headers["ETag"] = etag
        if partial:
            headers["Content-Range"] = f"bytes {start}-{end}/{expected_size}"
        status_code = 206 if partial else 200
        if request.method == "HEAD":
            return Response(content=b"", media_type=media_type, headers=headers, status_code=status_code)
        return StreamingResponse(body(), media_type=media_type, headers=headers, status_code=status_code)

    @router.get("/stat")
    async def stat(
        request: Request,
        path: str = Query(...),
        fingerprint: bool = Query(default=False),
    ) -> dict[str, Any]:
        """Return bounded metadata; hashing is opt-in and file-only."""
        _, service = app_client(request)
        try:
            return await service.request(
                "stat",
                path,
                {"include_fingerprint": fingerprint},
            )
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.get("/search")
    async def search(
        request: Request,
        path: str = Query(...),
        query: str = Query(..., min_length=1),
        content: bool = Query(default=False),
        max_results: int = Query(default=100, ge=1, le=1000),
        max_entries: int = Query(default=10000, ge=1, le=100000),
        max_depth: int = Query(default=32, ge=0, le=128),
        include_hidden: bool = Query(default=False),
        case_sensitive: bool = Query(default=False),
    ) -> dict[str, Any]:
        owner, service = app_client(request)
        operation = "content_search" if content else "filename_search"
        payload = {
            "query": query,
            "max_results": max_results,
            "max_entries": max_entries,
            "max_depth": max_depth,
            "max_bytes_per_file": 1024 * 1024,
            "include_hidden": include_hidden,
            "case_sensitive": case_sensitive,
        }
        try:
            return await service.request(operation, path, payload)
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/write")
    async def write(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner, service = app_client(request)
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        payload: dict[str, Any] = {"text": str(body.get("text") or body.get("content") or "")}
        expected = body.get("expected_fingerprint")
        if expected is not None:
            payload["expected_fingerprint"] = {"algorithm": "sha256", "value": str(expected)} if isinstance(expected, str) else expected
        try:
            return await service.request("replace", path, payload)
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/create")
    async def create(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Create a new file without replacing an existing path."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        payload: dict[str, Any] = {"text": str(body.get("text") or body.get("content") or "")}
        if body.get("bytes") is not None:
            payload = {"bytes": body["bytes"]}
        try:
            return await service.request("create", path, payload)
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/mkdir")
    async def mkdir(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Create one child directory under an authorized writable root."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            return await service.request("mkdir", path, {})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/trash")
    async def trash(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Move a file/folder into the Rust recoverable trash area."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            return await service.request("trash", path, {})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/restore")
    async def restore(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Restore one server-issued trash entry; clients cannot invent a path."""
        _, service = app_client(request)
        entry = body.get("entry")
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="entry is required")
        original = str(entry.get("original_path") or "").strip()
        if not original:
            raise HTTPException(status_code=400, detail="entry.original_path is required")
        try:
            return await service.request("restore", original, {"entry": entry})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/edit")
    async def edit(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner, service = app_client(request)
        path = str(body.get("path") or "").strip()
        if not path or "old" not in body:
            raise HTTPException(status_code=400, detail="path and old are required")
        payload = {"old": str(body.get("old") or ""), "new": str(body.get("new") or ""), "replace_all": bool(body.get("replace_all"))}
        expected = body.get("expected_fingerprint")
        if expected is not None:
            payload["expected_fingerprint"] = {"algorithm": "sha256", "value": str(expected)} if isinstance(expected, str) else expected
        try:
            return await service.request("patch", path, payload)
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/copy")
    async def copy(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Copy one regular file after Rust authorizes both paths."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        destination = str(body.get("destination") or body.get("to") or "").strip()
        if not path or not destination:
            raise HTTPException(status_code=400, detail="path and destination are required")
        try:
            return await service.request("copy", path, {"destination": destination})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/move")
    async def move(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Move one regular file after Rust authorizes both paths."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        destination = str(body.get("destination") or body.get("to") or "").strip()
        if not path or not destination:
            raise HTTPException(status_code=400, detail="path and destination are required")
        try:
            return await service.request("move", path, {"destination": destination})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.post("/rename")
    async def rename(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """Rename within one authorized root; Rust preserves atomic semantics."""
        _, service = app_client(request)
        path = str(body.get("path") or "").strip()
        destination = str(body.get("destination") or body.get("to") or "").strip()
        if not path or not destination:
            raise HTTPException(status_code=400, detail="path and destination are required")
        try:
            return await service.request("rename", path, {"destination": destination})
        except FilesServiceError as error:
            raise service_error(error) from error

    @router.get("/roots")
    async def list_roots(request: Request) -> dict[str, Any]:
        owner = authenticated_owner(request)
        try:
            return {"version": 1, "roots": registry.list(owner)}
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/roots")
    async def add_root(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner = authenticated_owner(request)
        path = str(body.get("path") or "")
        kind = str(body.get("kind") or "")
        capabilities = list(body.get("capabilities") or [])
        if not owner_is_admin_or_single_user(owner) and not assigned_root_allows(owner, path, kind, capabilities):
            raise HTTPException(status_code=403, detail="Agent root must stay inside assigned user-visible roots")
        try:
            root = registry.add(
                owner,
                path,
                kind,
                capabilities,
            )
            close_all_clients()
            return {"root": root}
        except FilesystemRegistryError as error:
            status = 409 if error.code == "duplicate_root" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    @router.patch("/roots/{root_id}")
    async def update_root(request: Request, root_id: str, body: dict[str, Any]) -> dict[str, Any]:
        owner = authenticated_owner(request)
        if not owner_is_admin_or_single_user(owner) and "capabilities" in body:
            existing = next((item for item in registry.list(owner) if item.get("id") == root_id), None)
            if not existing or not assigned_root_allows(owner, str(existing.get("canonical_path") or ""), str(existing.get("kind") or ""), list(body.get("capabilities") or [])):
                raise HTTPException(status_code=403, detail="Agent root capabilities exceed assigned user-visible roots")
        try:
            root = registry.update(
                owner,
                root_id,
                enabled=body.get("enabled") if "enabled" in body else None,
                capabilities=list(body["capabilities"]) if "capabilities" in body else None,
            )
            close_all_clients()
            return {"root": root}
        except FilesystemRegistryError as error:
            status = 404 if error.code == "root_not_found" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    @router.delete("/roots/{root_id}")
    async def delete_root(request: Request, root_id: str) -> dict[str, Any]:
        owner = authenticated_owner(request)
        try:
            registry.remove(owner, root_id)
            close_all_clients()
            return {"ok": True, "id": root_id}
        except FilesystemRegistryError as error:
            status = 404 if error.code == "root_not_found" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    @router.get("/visibility")
    async def list_visibility(request: Request) -> dict[str, Any]:
        owner = authenticated_owner(request)
        try:
            if owner_is_admin_or_single_user(owner):
                return {"version": 1, "generation": registry.app_scope(owner, is_admin=True)["generation"], "assignments": registry.list_visibility()}
            assignments = registry.visibility_for_subject(owner)
            return {"version": 1, "generation": registry.app_scope(owner, is_admin=False)["generation"], "assignments": assignments}
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error

    @router.get("/app-scope")
    async def app_scope(request: Request) -> dict[str, Any]:
        owner = authenticated_owner(request)
        try:
            return {"version": 1, "scope": registry.app_scope(owner, is_admin=owner_is_admin_or_single_user(owner))}
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error

    @router.get("/navigation-roots")
    async def navigation_roots(request: Request) -> dict[str, Any]:
        """Project the complete app-visible Files tree roots for this principal.

        This is deliberately not ``/roots``: that endpoint represents the
        caller's narrower Agent authority.  The Files explorer is user-directed
        app activity, so administrators receive the OS-visible namespace anchors
        and standard users receive only effective readable assignments.  The
        browser cannot supply or widen the projection.
        """
        owner, scope = app_context(request)
        try:
            return project_navigation_roots(registry, owner, scope)
        except FilesystemRegistryError as error:
            raise HTTPException(status_code=503, detail={"code": error.code, "message": str(error)}) from error

    @router.post("/visibility")
    async def add_visibility(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        owner = require_admin(request)
        try:
            assignment = registry.assign_visibility(
                owner,
                str(body.get("subject_id") or ""),
                str(body.get("root_id") or ""),
                list(body.get("capabilities") or []),
                subject_kind=str(body.get("subject_kind") or "user"),
            )
            close_all_clients()
            return {"assignment": assignment}
        except FilesystemRegistryError as error:
            status = 409 if error.code == "duplicate_assignment" else 404 if error.code == "root_not_found" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    @router.patch("/visibility/{assignment_id}")
    async def update_visibility(request: Request, assignment_id: str, body: dict[str, Any]) -> dict[str, Any]:
        owner = require_admin(request)
        try:
            assignment = registry.update_visibility(
                owner,
                assignment_id,
                enabled=body.get("enabled") if "enabled" in body else None,
                capabilities=list(body["capabilities"]) if "capabilities" in body else None,
            )
            close_all_clients()
            return {"assignment": assignment}
        except FilesystemRegistryError as error:
            status = 404 if error.code == "assignment_not_found" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    @router.delete("/visibility/{assignment_id}")
    async def delete_visibility(request: Request, assignment_id: str) -> dict[str, Any]:
        owner = require_admin(request)
        try:
            registry.remove_visibility(owner, assignment_id)
            close_all_clients()
            return {"ok": True, "id": assignment_id}
        except FilesystemRegistryError as error:
            status = 404 if error.code == "assignment_not_found" else 400
            raise HTTPException(status_code=status, detail={"code": error.code, "message": str(error)}) from error

    return router

"""Owner-bound, revision-bound sessions for interactive Files previews."""

from __future__ import annotations

import hashlib
import io
import zipfile
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Mapping

from src.constants import DATA_DIR
from src.openclank.browser_widget_state import declaration as widget_declaration
from src.openclank.browser_preview_origin import (
    BrowserPreviewOrigin,
    BrowserPreviewOriginError,
    PreviewAsset,
    PreviewLease,
)


class BrowserPreviewError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        self.code = str(code)
        self.status_code = int(status_code)
        super().__init__(message)


def revision_fingerprint(value: Mapping | None, content_digest: str) -> str:
    normalized = dict(value or {})
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return f"sha256:{digest}:{content_digest}"


class BrowserPreviewService:
    """Own active leases and a durable origin/opaque-host manifest.

    Only the bound loopback listener and a hash-to-random-host manifest survive
    process restarts. Resource refs and preview bytes are kept in memory only.
    """

    MAX_HTML_BYTES = 8 * 1024 * 1024
    MAX_DEPENDENCIES = 32
    SESSION_TTL_SECONDS = 15 * 60

    def __init__(self, data_dir: str | Path = DATA_DIR, *, origin: BrowserPreviewOrigin | None = None):
        self.origin = origin or BrowserPreviewOrigin(data_dir, max_asset_bytes=self.MAX_HTML_BYTES)
        self._client_generation_lock = threading.RLock()
        self._client_generations: dict[tuple[str, str], tuple[int, float]] = {}
        self._client_cancellations: dict[tuple[str, str], tuple[int, float]] = {}
        self._client_publications: dict[tuple[str, str], tuple[int, str]] = {}

    def note_client_generation(self, *, owner_subject_id: str, client_id: str, generation: int) -> bool:
        owner = str(owner_subject_id or "").strip()
        client = str(client_id or "").strip()
        value = int(generation)
        if not owner or not re.fullmatch(r"[0-9a-f]{32}", client) or value < 1:
            raise BrowserPreviewError("invalid_preview_request", "Browser preview request identity is invalid.")
        now = time.monotonic()
        key = (owner, client)
        with self._client_generation_lock:
            for stale_key, (_stale_generation, changed_at) in list(self._client_generations.items()):
                if now - changed_at > 3600:
                    self._client_generations.pop(stale_key, None)
                    self._client_cancellations.pop(stale_key, None)
                    self._client_publications.pop(stale_key, None)
            previous = self._client_generations.get(key)
            cancellation = self._client_cancellations.get(key)
            if (previous is not None and value <= previous[0]) or (cancellation is not None and value <= cancellation[0]):
                return False
            self._client_generations[key] = (value, now)
            publication = self._client_publications.get(key)
            if publication is not None and publication[0] < value:
                revoked = self.origin.revoke(session_id=publication[1], owner_subject_id=owner)
                if not revoked and self.origin.current_lease(session_id=publication[1], owner_subject_id=owner) is not None:
                    return False
                if self._client_publications.get(key) == publication:
                    self._client_publications.pop(key, None)
            return True

    def cancel_client_generation(self, *, owner_subject_id: str, client_id: str, generation: int) -> bool:
        owner = str(owner_subject_id or "").strip()
        client = str(client_id or "").strip()
        value = int(generation)
        if not owner or not re.fullmatch(r"[0-9a-f]{32}", client) or value < 1:
            raise BrowserPreviewError("invalid_preview_request", "Browser preview request identity is invalid.")
        key = (owner, client)
        now = time.monotonic()
        with self._client_generation_lock:
            latest = self._client_generations.get(key)
            if latest is None or value > latest[0]:
                self._client_generations[key] = (value, now)
            previous_cancellation = self._client_cancellations.get(key)
            if previous_cancellation is None or value > previous_cancellation[0]:
                self._client_cancellations[key] = (value, now)
            publication = self._client_publications.get(key)
            if publication is not None and publication[0] <= value:
                revoked = self.origin.revoke(session_id=publication[1], owner_subject_id=owner)
                if not revoked and self.origin.current_lease(session_id=publication[1], owner_subject_id=owner) is not None:
                    return False
                if self._client_publications.get(key) == publication:
                    self._client_publications.pop(key, None)
            return True

    def create(
        self,
        *,
        owner_subject_id: str,
        workspace_id: str,
        resource_key: str,
        revision: Mapping | None,
        html: bytes,
        app_origin: str,
        dependencies: Mapping[str, PreviewAsset] | None = None,
        client_reported_warnings: tuple[str, ...] = (),
        validate_current: Callable[[str], bool] | None = None,
        client_id: str | None = None,
        client_generation: int | None = None,
        now: float | None = None,
    ) -> dict:
        owner = str(owner_subject_id or "").strip()
        workspace = str(workspace_id or "default").strip()
        resource = str(resource_key or "").strip()
        if not owner or not workspace or not resource:
            raise BrowserPreviewError("invalid_preview_scope", "Owner, workspace, and resource identity are required.")
        if not isinstance(html, bytes) or not html or len(html) > self.MAX_HTML_BYTES:
            raise BrowserPreviewError("preview_too_large", "HTML preview is empty or exceeds the configured limit.", status_code=413)
        if not isinstance(revision, (Mapping, type(None))):
            raise BrowserPreviewError("invalid_revision", "Resource revision is invalid.")
        dependency_rows = dict(dependencies or {})
        if (not isinstance(client_reported_warnings, (tuple, list)) or len(client_reported_warnings) > 16
                or any(not isinstance(item, str) or len(item) > 256 or any(ord(ch) < 32 or ord(ch) == 127 for ch in item) for item in client_reported_warnings)):
            raise BrowserPreviewError("invalid_preview_request", "Preview diagnostics are invalid.")
        if len(dependency_rows) > self.MAX_DEPENDENCIES:
            raise BrowserPreviewError("invalid_dependency_manifest", "Too many preview dependencies.")
        content_digest = "sha256:" + hashlib.sha256(html).hexdigest()
        rev = revision_fingerprint(revision, content_digest)
        scope_key = hashlib.sha256("\0".join((owner, workspace, resource)).encode("utf-8")).hexdigest()
        current_time = time.time() if now is None else float(now)
        request_key = None
        if client_id is not None or client_generation is not None:
            client = str(client_id or "")
            generation = int(client_generation or 0)
            request_key = (owner, client)
            with self._client_generation_lock:
                latest = self._client_generations.get(request_key)
                cancellation = self._client_cancellations.get(request_key)
                if (
                    latest is None or latest[0] != generation
                    or (cancellation is not None and generation <= cancellation[0])
                    or time.monotonic() - latest[1] > 3600
                ):
                    raise BrowserPreviewError("stale_preview_request", "A newer, cancelled, or expired Browser preview request superseded this one.", status_code=409)
                previous_publication = self._client_publications.get(request_key)
                if previous_publication and previous_publication[0] < generation:
                    self.origin.revoke(session_id=previous_publication[1], owner_subject_id=owner)
                try:
                    lease = self.origin.publish(
                        scope_key=scope_key,
                        owner_subject_id=owner,
                        workspace_id=workspace,
                        resource_key=resource,
                        revision=rev,
                        digest=content_digest,
                        html=html,
                        app_origin=app_origin,
                        dependencies=dependency_rows,
                        client_reported_warnings=tuple(client_reported_warnings),
                        expires_at=current_time + self.SESSION_TTL_SECONDS,
                        validate_current=validate_current,
                    )
                except BrowserPreviewOriginError as exc:
                    raise BrowserPreviewError(exc.code, str(exc), status_code=503) from exc
                self._client_publications[request_key] = (generation, lease.session_id)
        else:
            try:
                lease = self.origin.publish(
                    scope_key=scope_key,
                    owner_subject_id=owner,
                    workspace_id=workspace,
                    resource_key=resource,
                    revision=rev,
                    digest=content_digest,
                    html=html,
                    app_origin=app_origin,
                    dependencies=dependency_rows,
                    client_reported_warnings=tuple(client_reported_warnings),
                    expires_at=current_time + self.SESSION_TTL_SECONDS,
                    validate_current=validate_current,
                )
            except BrowserPreviewOriginError as exc:
                raise BrowserPreviewError(exc.code, str(exc), status_code=503) from exc
        return self._public_session(lease)

    def export_source_bundle(self, *, session_id: str, owner_subject_id: str) -> tuple[bytes, PreviewLease]:
        """Copy only this live lease's declared bytes; never resolve dependencies."""
        lease = self.origin.current_lease(session_id=session_id, owner_subject_id=owner_subject_id)
        if lease is None:
            raise BrowserPreviewError("preview_unavailable", "Browser preview session is unavailable.", status_code=404)
        if lease.validate_current is None:
            raise BrowserPreviewError("preview_unavailable", "Current preview authority is unavailable.", status_code=409)
        entries = [("index.html", lease.html, "text/html", lease.digest)]
        names = {"index.html", "openclank-manifest.json"}
        for path, asset in sorted(lease.dependencies.items()):
            parts = path.split("/") if isinstance(path, str) else []
            folded = path.casefold() if isinstance(path, str) else ""
            if (not self.origin._safe_dependency_path(path) or ":" in path
                    or any(ord(ch) < 32 or ord(ch) == 127 for ch in path)
                    or folded in names or any(folded.startswith(name + "/") or name.startswith(folded + "/") for name in names)):
                raise BrowserPreviewError("invalid_dependency_manifest", "Preview dependency paths cannot be bundled safely.", status_code=409)
            names.add(folded)
            if not isinstance(asset, PreviewAsset) or not isinstance(asset.data, bytes):
                raise BrowserPreviewError("invalid_dependency_manifest", "Preview dependency is invalid.", status_code=409)
            entries.append((path, asset.data, asset.media_type, asset.digest))
        if len(entries) > self.MAX_DEPENDENCIES + 1 or sum(len(data) for _, data, _, _ in entries) > 16 * 1024 * 1024:
            raise BrowserPreviewError("preview_too_large", "Preview bundle exceeds its byte limit.", status_code=413)
        def check():
            if self.origin.current_lease(session_id=session_id, owner_subject_id=owner_subject_id) is not lease:
                raise BrowserPreviewError("preview_stale", "Browser preview session changed.", status_code=409)
            for path, data, _media, digest in entries:
                try: valid = lease.validate_current("" if path == "index.html" else path)
                except Exception: valid = False
                if not valid or digest != "sha256:" + hashlib.sha256(data).hexdigest():
                    raise BrowserPreviewError("preview_stale", "Browser preview source or dependency changed.", status_code=409)
                if self.origin.current_lease(session_id=session_id, owner_subject_id=owner_subject_id) is not lease:
                    raise BrowserPreviewError("preview_stale", "Browser preview session changed.", status_code=409)
        check()
        manifest = {"version": 1, "kind": "declared_source_bundle", "coverage": "partial_static",
                    "limitations": ["Only declared source bytes are included; dynamic JavaScript, fetches and runtime dependencies are not verified."],
                    "client_reported_warnings": list(lease.client_reported_warnings),
                    "components": [{"path": path, "sha256": digest, "size": len(data), "media_type": media}
                                   for path, data, media, digest in entries]}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
            for path, data, _media, _digest in entries: archive.writestr(path, data)
            archive.writestr("openclank-manifest.json", json.dumps(manifest, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
        result = output.getvalue()
        if len(result) > 17 * 1024 * 1024:
            raise BrowserPreviewError("preview_too_large", "Preview bundle exceeds its output limit.", status_code=413)
        check()
        return result, lease

    def revoke(self, *, session_id: str, owner_subject_id: str) -> bool:
        return self.origin.revoke(session_id=session_id, owner_subject_id=owner_subject_id)

    def check_status(self, *, session_id: str, owner_subject_id: str) -> dict | None:
        lease = self.origin.current_lease(session_id=session_id, owner_subject_id=owner_subject_id)
        if lease is not None and lease.validate_current is not None:
            try:
                valid = bool(lease.validate_current(""))
            except Exception:
                valid = False
            if not valid:
                self.origin.retire(lease, "stale")
        return self.origin.session_status(session_id=session_id, owner_subject_id=owner_subject_id)

    def close(self) -> None:
        self.origin.stop()

    def _public_session(self, lease: PreviewLease) -> dict:
        return {
            "session_id": lease.session_id,
            "artifact_id": lease.artifact_id,
            "resource_key": lease.resource_key,
            "workspace_id": lease.workspace_id,
            "revision": lease.revision,
            "digest": lease.digest,
            "url": self.origin.url_for(lease),
            "origin_port": self.origin.port,
            "expires_at": lease.expires_at,
            "widget_state": widget_declaration(lease.html),
        }


__all__ = ["BrowserPreviewError", "BrowserPreviewService", "revision_fingerprint"]

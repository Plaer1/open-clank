"""Rust-backed Host provider for the canonical Files facade.

This compatibility adapter moves Host metadata behind opaque ResourceRefs now.
It never walks, stats, or opens the host filesystem in Python. Every target is
resolved from an encrypted origin and re-authorized by the existing Rust app
lane. Descriptor handles/Tonic remain the production content replacement.
"""

from __future__ import annotations

import base64
import hashlib
import fnmatch
import json
import logging
import mimetypes
import os
import re
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from src.openclank.files_facade import (
    FilesFacadeError,
    ProviderContent,
    ProviderContext,
    ProviderPage,
    ProviderResource,
    ProviderWorkspaceTarget,
    SORT_KEYS,
)
from src.openclank.files_service_client import FilesServiceError, client_for_owner
from src.openclank.filesystem_registry import FilesystemRegistryError, FilesystemRootRegistry
from src.openclank.host_apps import host_apps, HostAppsError
from routes.odysseus_files_routes import project_navigation_roots
from src.openclank.resource_refs import issue_resource_ref, stable_resource_id


_ORIGIN_PREFIX = "host:v1:"
_LOG = logging.getLogger(__name__)


def _origin(path: str) -> str:
    path = str(path)
    if sys.platform == "win32":
        # Rust canonical stat/list paths are verbatim; registry anchors are DOS.
        # Keep native verbatim I/O semantics and fold only their prefix aliases,
        # never filename case or distinct canonical resources.
        if re.match(r"^[A-Za-z]:\\", path):
            path = "\\\\?\\" + path[0].upper() + path[1:]
        elif re.match(r"^\\\\\?\\[A-Za-z]:\\", path):
            path = path[:4] + path[4].upper() + path[5:]
        elif path.startswith("\\\\") and not path.startswith(("\\\\?\\", "\\\\.\\")):
            path = "\\\\?\\UNC\\" + path[2:]
    payload = json.dumps({"path": path}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _ORIGIN_PREFIX + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def host_origin_for_path(path: str) -> str:
    """Derive the same Host origin used by Files from a trusted server path.

    This is an identity comparison helper only. Callers must still resolve the
    path through ``HostFilesProvider.resource_for_path`` before authorizing it.
    """
    return _origin(os.path.realpath(str(path)))


def _path(origin_id: str) -> str:
    raw = str(origin_id or "")
    if not raw.startswith(_ORIGIN_PREFIX):
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
    token = raw[len(_ORIGIN_PREFIX):]
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        value = json.loads(decoded.decode("utf-8"))
        path = str(value["path"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable") from exc
    if not path or "\x00" in path or not Path(path).is_absolute():
        raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
    return path


def _service_error(error: FilesServiceError) -> FilesFacadeError:
    code = {
        "denied": "resource_unavailable",
        "invalid_path": "resource_unavailable",
        "policy_generation_changed": "resource_ref_stale",
        "conflict": "resource_changed",
        "stale_cursor": "stale_cursor",
        "backpressure": "provider_unavailable",
    }.get(error.code, "provider_unavailable")
    return FilesFacadeError("Host resource is unavailable", code=code)


def _detected_mime(name: str, first_bytes: bytes) -> str:
    """Classify a small prefix while retaining extension detection provenance."""
    if first_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if first_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if first_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if first_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return mimetypes.guess_type(str(name))[0] or "application/octet-stream"


# Generated from the shared first-party registry at editor build time. This is
# an open-target hint only; Rust's bounded read still rejects binary/unsupported
# text, and every operation retains the existing opaque-ref authorization.
try:
    _EDITOR_ASSOCIATIONS = json.loads(Path(__file__).with_name("editor_language_associations.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    _EDITOR_ASSOCIATIONS = {}
_EDITOR_BINARY_SUFFIX = re.compile(_EDITOR_ASSOCIATIONS.get("binarySuffixPattern", r"(?!)"), re.IGNORECASE)


class HostFilesProvider:
    name = "host"
    _ROOT_SORTS = ("name", "kind")
    _EDITOR_MIME_TYPES = frozenset({
        "application/javascript", "application/json", "application/ld+json", "application/xml",
        "text/css", "text/html", "text/markdown", "text/plain", "text/x-c", "text/x-python",
        "text/x-rust", "text/x-shellscript", "text/xml",
    })

    async def operation_status(
        self,
        context: ProviderContext,
        *,
        operation_id: str,
        destination_origin_id: str | None = None,
        name: str | None = None,
        collision: str | None = None,
        digest: str | None = None,
        length: int | None = None,
        request_digest: str | None = None,
        destination_revision: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        """Reconstruct a committed import from the durable provider marker."""
        self._scope(context)
        loaded = self._operation_load(context, operation_id)
        if not loaded:
            loaded = self._directory_operation_load(context, operation_id)
        if loaded and loaded.get("request_kind") == "create-directory":
            return await self._directory_status(context, loaded)
        if not loaded:
            return None
        if loaded.get("operation_id") != operation_id or loaded.get("generation") != int(context.policy_generation):
            return None
        if loaded.get("workspace_id", "default") != str(getattr(context, "workspace_id", "default") or "default"):
            return None
        if destination_origin_id is not None and loaded.get("destination") != _path(destination_origin_id):
            return None
        if name is not None and loaded.get("name") != name:
            return None
        if collision is not None and loaded.get("collision") != collision:
            return None
        if digest is not None and loaded.get("digest") != digest:
            return None
        if length is not None and loaded.get("length") != length:
            return None
        if request_digest is not None and loaded.get("request_digest") != request_digest:
            return None
        if destination_revision is not None and loaded.get("destination_revision") != dict(destination_revision):
            return None
        if loaded.get("phase") not in {"pending", "complete"}:
            return None
        if loaded.get("phase") == "pending" and not self._ownership_proven(loaded):
            # A matching file is not evidence that this operation created it.
            # Pending recovery requires the exclusive sidecar claimed before
            # stage_finish, otherwise a pre-existing file can be adopted.
            return None
        target = str(loaded.get("target") or "")
        item_id = str(loaded.get("item_id") or "")
        if not target or not item_id:
            return None
        try:
            created = await self.stat(context, origin_id=_origin(target))
        except Exception:
            return None
        if created.kind != "file" or (loaded.get("length") is not None and created.size is not None and int(created.size) != int(loaded["length"])):
            return None
        revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
        before_kind = str(loaded.get("target_before_kind") or "")
        before_revision = loaded.get("target_before_revision")
        if before_kind and before_kind != "file":
            return None
        if before_kind == "file" and isinstance(before_revision, Mapping) and revision == dict(before_revision):
            # The target was already this exact file before the operation's
            # exclusive claim; stage_finish did not commit this operation.
            return None
        expected_digest = str(loaded.get("digest") or "")
        expected_value = expected_digest.removeprefix("sha256:") if expected_digest.startswith("sha256:") else ""
        if expected_value and (not revision or revision.get("kind") != "hostFingerprint" or revision.get("value") != expected_value):
            # A same-sized pre-existing or externally replaced file must not
            # be mistaken for the result of this lost operation.
            return None
        if loaded.get("phase") == "pending":
            # ``stage_finish`` may have committed the target before the
            # provider marker or facade receipt was updated. Promote the
            # exact pending marker so subsequent retries see the same
            # operation, including collision=rename's selected target.
            completed_marker = dict(loaded)
            completed_marker.update({"phase": "complete", "revision": revision})
            try:
                self._operation_save(context, operation_id, expected_digest, completed_marker)
                self._release_ownership(loaded)
            except Exception:
                # The facade still receives the reconciled receipt. A later
                # retry can repeat this idempotent promotion if persistence
                # itself was the crashed step.
                pass
        item = {
            "item_id": item_id, "outcome": "committed",
            "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id),
            "resource_ref": issue_resource_ref(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id, kind=created.kind, capabilities=created.capabilities, policy_generation=context.policy_generation).token,
            "receipt_id": operation_id,
        }
        if revision:
            item["revision"] = revision
        item["provenance"] = {
            "domain": "host",
            "source_digest": expected_digest,
            "selected_name": loaded.get("selected_name") or Path(target).name,
            "declared_mime": loaded.get("declared_mime"),
            "detected_mime": loaded.get("detected_mime"),
        }
        item["history"] = {
            "receipt_id": operation_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {
                "receipt_id": operation_id,
                "outcome": "committed",
                "selected_name": loaded.get("selected_name") or Path(target).name,
                "revision": revision,
            },
        }
        return {"state": "complete", "items": [item], "_request_digest": loaded.get("request_digest")}
    # Base snapshots are metadata-only and deliberately bounded.  The limit
    # is a safety ceiling, not an indication that the corpus is complete.
    _BASE_MAX_ROWS = 2_000
    _BASE_MAX_DEPTH = 32
    _BASE_MAX_PAGES_PER_FOLDER = 64

    def __init__(
        self,
        *,
        registry: FilesystemRootRegistry | None = None,
        client_factory: Callable[..., Any] = client_for_owner,
        operation_store: Any | None = None,
    ) -> None:
        self.registry = registry or FilesystemRootRegistry()
        self.client_factory = client_factory
        self.operation_store = operation_store

    @staticmethod
    def _operation_key(operation_id: str) -> str:
        return f"__host_import__{operation_id}"

    def _operation_load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=self._operation_key(operation_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _operation_save(self, context: ProviderContext, operation_id: str, digest: str, marker: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if callable(recorder):
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._operation_key(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(marker))

    @staticmethod
    def _directory_operation_key(operation_id: str) -> str:
        return f"__host_directory__{operation_id}"

    def _directory_operation_load(self, context: ProviderContext, operation_id: str) -> dict[str, Any] | None:
        getter = getattr(self.operation_store, "get_operation", None)
        if not callable(getter):
            return None
        loaded = getter(owner_subject_id=context.owner_subject_id, operation_id=self._directory_operation_key(operation_id))
        return dict(loaded.get("receipt") or {}) if isinstance(loaded, Mapping) else None

    def _directory_operation_save(self, context: ProviderContext, operation_id: str, digest: str, marker: Mapping[str, Any]) -> None:
        recorder = getattr(self.operation_store, "record_operation", None)
        if callable(recorder):
            recorder(owner_subject_id=context.owner_subject_id, operation_id=self._directory_operation_key(operation_id), request_digest=digest, generation=int(context.policy_generation), receipt=dict(marker))

    async def _directory_status(self, context: ProviderContext, loaded: Mapping[str, Any]) -> Mapping[str, Any] | None:
        if loaded.get("generation") != int(context.policy_generation) or loaded.get("workspace_id", "default") != str(getattr(context, "workspace_id", "default") or "default"):
            return None
        if loaded.get("phase") not in {"pending", "complete"}:
            return None
        target = str(loaded.get("target") or "")
        parent = str(loaded.get("parent") or "")
        name = str(loaded.get("name") or "")
        if not target or not parent or not name or os.path.dirname(target) != parent or os.path.basename(target) != name:
            return None
        if loaded.get("phase") == "pending" and not self._ownership_proven(loaded):
            return None
        try:
            created = await self.stat(context, origin_id=_origin(target))
        except Exception:
            return None
        if created.kind != "folder" or created.parent_origin_id != _origin(parent):
            return None
        before_kind = str(loaded.get("target_before_kind") or "")
        if before_kind and loaded.get("outcome") != "unchanged":
            return None
        revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
        if loaded.get("phase") == "pending":
            complete = {**dict(loaded), "phase": "complete", "revision": revision}
            try:
                self._directory_operation_save(context, str(loaded.get("operation_id") or ""), str(loaded.get("request_digest") or ""), complete)
                self._release_ownership(loaded)
            except Exception:
                pass
        item = {
            "item_id": str(loaded.get("item_id") or loaded.get("operation_id") or ""),
            "outcome": "committed",
            "resource_key": stable_resource_id(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id),
            "resource_ref": issue_resource_ref(owner_subject_id=context.owner_subject_id, provider=self.name, origin_id=created.origin_id, kind=created.kind, capabilities=created.capabilities, policy_generation=context.policy_generation).token,
            "receipt_id": str(loaded.get("operation_id") or ""),
        }
        if revision:
            item["revision"] = revision
        return {"state": "complete", "items": [item], "_request_digest": loaded.get("request_digest")}

    async def _directory_resource(self, context: ProviderContext, marker: Mapping[str, Any]) -> ProviderResource:
        target = str(marker.get("target") or "")
        created = await self.stat(context, origin_id=_origin(target))
        if created.kind != "folder" or created.parent_origin_id != _origin(str(marker.get("parent") or "")):
            raise FilesFacadeError("Host directory operation is unavailable", code="resource_changed")
        receipt_id = str(marker.get("operation_id") or uuid.uuid4().hex)
        outcome = "unchanged" if marker.get("outcome") == "unchanged" else "created"
        return replace(created, action_receipt={
            "action_id": receipt_id,
            "receipt_id": receipt_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {"action_id": receipt_id, "receipt_id": receipt_id, "outcome": outcome},
        })

    def _import_marker(
        self,
        context: ProviderContext,
        *,
        operation_id: str,
        item_id: str,
        target: str,
        length: int,
        digest: str,
        stage: Mapping[str, Any],
        phase: str = "pending",
        revision: Mapping[str, Any] | None = None,
        request_digest: str | None = None,
        destination: str | None = None,
        destination_revision: Mapping[str, Any] | None = None,
        name: str | None = None,
        collision: str | None = None,
        selected_name: str | None = None,
        ownership_path: str | None = None,
        ownership_token: str | None = None,
        target_before_kind: str | None = None,
        target_before_revision: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the private marker used to recover a committed stage.

        The absolute target is retained only in the provider's encrypted
        operation store.  It lets a fresh provider stat the exact destination
        after ``stage_finish`` succeeds but the facade ledger write is lost.
        """
        marker: dict[str, Any] = {
            "operation_id": str(operation_id),
            "item_id": str(item_id),
            "generation": int(context.policy_generation),
            "workspace_id": str(getattr(context, "workspace_id", "default") or "default"),
            "phase": str(phase),
            "destination": str(destination or Path(target).parent),
            "target": str(target),
            "name": str(name or Path(target).name),
            "collision": str(collision or "fail"),
            "length": int(length),
            "digest": str(digest),
            "request_digest": str(request_digest or ""),
            "destination_revision": dict(destination_revision) if destination_revision is not None else None,
            "selected_name": str(selected_name or Path(target).name),
            "declared_mime": stage.get("declared_mime"),
            "detected_mime": stage.get("detected_mime"),
            "ownership_path": str(ownership_path or ""),
            "ownership_token": str(ownership_token or ""),
            "target_before_kind": str(target_before_kind or ""),
            "target_before_revision": dict(target_before_revision) if target_before_revision is not None else None,
        }
        if revision is not None:
            marker["revision"] = dict(revision)
        return marker

    @staticmethod
    def _ownership_sidecar(destination: str, operation_id: str, candidate: str, digest: str, *, owner_id: str = "", workspace_id: str = "default") -> str:
        key = hashlib.sha256(f"{owner_id}\x00{workspace_id}\x00{destination}\x00{operation_id}\x00{candidate}\x00{digest}".encode("utf-8")).hexdigest()[:40]
        root = Path(tempfile.gettempdir()) / ".openclank-host-import-owners-v1"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return str(root / f"{key}.owner")

    @staticmethod
    def _claim_ownership(path: str) -> tuple[str, str]:
        token = uuid.uuid4().hex
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        mode = 0o600
        descriptor = os.open(path, flags, mode)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                descriptor = -1
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return path, token

    @staticmethod
    def _release_ownership(marker: Mapping[str, Any]) -> None:
        path = str(marker.get("ownership_path") or "")
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _ownership_proven(marker: Mapping[str, Any]) -> bool:
        path = str(marker.get("ownership_path") or "")
        token = str(marker.get("ownership_token") or "")
        if not path or not token:
            return False
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            try:
                value = os.read(descriptor, 128).decode("ascii")
            finally:
                os.close(descriptor)
            return value == token
        except (OSError, UnicodeDecodeError):
            return False

    def _scope(self, context: ProviderContext) -> dict[str, Any]:
        try:
            return self.registry.app_scope(context.owner_username, is_admin=context.is_admin)
        except FilesystemRegistryError as exc:
            raise FilesFacadeError("Host policy is unavailable", code="provider_unavailable") from exc

    def _client(self, context: ProviderContext, scope: dict[str, Any]):
        if self.client_factory is client_for_owner:
            return self.client_factory(
                context.owner_username,
                app_scope=None if scope.get("host") else scope,
                transport="grpc" if sys.platform in {"darwin", "win32"} else "framed",
            )
        return self.client_factory(context.owner_username) if scope.get("host") else self.client_factory(
            context.owner_username, app_scope=scope,
        )

    def _stream_client(self, context: ProviderContext, scope: dict[str, Any]):
        if self.client_factory is client_for_owner:
            return self.client_factory(
                context.owner_username,
                app_scope=None if scope.get("host") else scope,
                transport="grpc" if sys.platform in {"darwin", "win32"} else "framed",
            )
        return self._client(context, scope)

    def supported_sort_keys(self, *, parent_origin_id: str) -> tuple[str, ...]:
        if parent_origin_id == "host:v1:root":
            return self._ROOT_SORTS
        _path(parent_origin_id)
        return SORT_KEYS

    @staticmethod
    def _caps(kind: str, *, writable: bool = False) -> tuple[str, ...]:
        # Capabilities describe facade operations that work today. Text files
        # can be opened through the same Rust read-lines service used by Code;
        # writes still require the caller's existing app-scoped service grant.
        if kind in {"folder", "directory", "recursive_directory"}:
            return ("children", "stat", "search", *(('watch',) if sys.platform in {"darwin", "win32"} else ()), *(('write',) if writable else ()))
        if kind == "file":
            return ("stat", "preview", "download", "open", *(('write', 'rename', 'move') if writable else ()))
        return ("stat",)

    def _path_writable(self, context: ProviderContext, path: str, scope: Mapping[str, Any] | None = None) -> bool:
        if scope is None:
            scope = self._scope(context)
        if scope.get("host"):
            return True
        if "write" not in set(scope.get("capabilities") or []):
            return False
        visible_root_ids = set(scope.get("visible_root_ids") or [])
        root_capabilities = scope.get("root_capabilities") or {}
        target = os.path.realpath(str(path))
        for assignment in self.registry.visibility_for_subject(context.owner_username):
            root = assignment.get("root") or {}
            root_id = str(assignment.get("root_id") or "")
            if root_id not in visible_root_ids or "write" not in set(root_capabilities.get(root_id) or []):
                continue
            if not root.get("enabled") or root.get("availability") != "available":
                continue
            root_path = str(root.get("canonical_path") or "")
            if not root_path or "write" not in set(assignment.get("capabilities") or []) or "write" not in set(root.get("capabilities") or []):
                continue
            try:
                if root.get("kind") == "exact_file":
                    if target == root_path:
                        return True
                elif root.get("kind") == "recursive_directory" and os.path.commonpath([root_path, target]) == root_path:
                    return True
            except ValueError:
                continue
        return False

    @classmethod
    def _editor_file(cls, name: str, mime_type: str | None = None) -> bool:
        mime = str(mime_type or "").strip().lower()
        path = str(name).replace("\\", "/").lower()
        leaf = path.rsplit("/", 1)[-1]
        if _EDITOR_BINARY_SUFFIX.search(leaf):
            return False
        if mime in cls._EDITOR_MIME_TYPES or mime.startswith("text/"):
            return True
        if leaf in _EDITOR_ASSOCIATIONS.get("filenames", ()):
            return True
        if any(leaf.endswith(suffix) for suffix in _EDITOR_ASSOCIATIONS.get("suffixes", ())):
            return True
        if any(leaf.endswith(suffix) for suffix in _EDITOR_ASSOCIATIONS.get("conditionalSuffixes", ())):
            return True
        return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(leaf, pattern)
                   for pattern in _EDITOR_ASSOCIATIONS.get("patterns", ()))

    @classmethod
    def _representation(cls, name: str, data: Mapping[str, Any]) -> str:
        # Files may make an explicit representation decision. This keeps a
        # provider's supported Markdown classification authoritative while
        # allowing a text adapter to keep a .md source file in source mode.
        explicit = str(data.get("representation") or "").strip()
        if explicit in {"markdown", "text"}:
            return explicit
        mime = str(data.get("media_type") or data.get("mime_type") or mimetypes.guess_type(name)[0] or "").lower()
        if mime == "text/markdown" or Path(name).suffix.lower() in {".markdown", ".mdown", ".md", ".mdx"}:
            return "markdown"
        return "text"

    @staticmethod
    def _fingerprint_value(data: Mapping[str, Any]) -> str:
        fingerprint = data.get("fingerprint")
        if isinstance(fingerprint, Mapping):
            return str(fingerprint.get("value") or "").strip()
        return str(fingerprint or "").strip()

    @staticmethod
    def _metadata(data: Mapping[str, Any]) -> dict[str, Any]:
        metadata = {
            key: data[key]
            for key in ("encoding", "newline", "language", "mode")
            if data.get(key) is not None
        }
        if data.get("bom_bytes") is not None:
            metadata["bomBytes"] = data["bom_bytes"]
        return metadata

    @classmethod
    def _snapshot(cls, name: str, data: Mapping[str, Any], representation: str) -> dict[str, Any]:
        text = data.get("text")
        fingerprint = cls._fingerprint_value(data)
        if not isinstance(text, str) or not fingerprint:
            raise FilesFacadeError("Host text resource snapshot is unavailable", code="provider_unavailable")
        return {
            "revision": {"kind": "hostFingerprint", "value": fingerprint},
            "envelope": {
                "text": text,
                "metadata": cls._metadata(data),
                "representation": representation,
            },
        }

    def _resource(self, path: str, *, name: str, kind: str, size: int | None = None,
                  modified_unix_ms: int | None = None, provenance: Mapping[str, Any] | None = None,
                  writable: bool = False, revision: Mapping[str, Any] | None = None,
                  action_receipt: Mapping[str, Any] | None = None,
                  native_available: bool = False, native_icon_available: bool = False) -> ProviderResource:
        normalized_kind = {
            "directory": "folder",
            "recursive_directory": "folder",
            "file": "file",
            "exact_file": "file",
            "symlink": "symlink",
            "other": "special",
        }.get(str(kind).lower(), "special")
        mime_type = None if normalized_kind != "file" else mimetypes.guess_type(name)[0]
        return ProviderResource(
            _origin(path),
            name,
            normalized_kind,
            self._caps(normalized_kind, writable=writable),
            # Physical roots have no parent; serializing / as its own parent
            # creates a cycle in the facade's authorized reveal ancestry.
            parent_origin_id=_origin(str(Path(path).parent)) if normalized_kind in {"file", "folder"} and Path(path).parent != Path(path) else None,
            mime_type=mime_type,
            size=size,
            modified_unix_ms=modified_unix_ms,
            provenance=dict(provenance or {"domain": "host"}),
            open_target={"app": "editor"} if normalized_kind == "file" and HostFilesProvider._editor_file(name, mime_type) else None,
            sort_kind=mime_type or normalized_kind,
            native_thumbnail_available=(sys.platform == "darwin" or native_available) if normalized_kind == "file" else False,
            native_icon_available=native_icon_available if normalized_kind == "file" else False,
            child_sort_keys=SORT_KEYS if "children" in self._caps(normalized_kind, writable=writable) else (),
            action_receipt=action_receipt,
            revision=revision,
        )

    async def create_directory(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        name: str,
        operation_id: str | None = None,
        request_digest: str | None = None,
        parent_revision: Mapping[str, Any] | None = None,
        collision: str = "fail",
    ) -> ProviderResource:
        """Create one child directory with an ownership marker before mkdir."""
        parent = _path(parent_origin_id)
        folder = str(name or "").strip()
        if (
            not folder or len(folder.encode("utf-8")) > 240 or "\x00" in folder
            or "/" in folder or "\\" in folder or folder in {".", ".."}
            or Path(folder).name != folder
        ):
            raise FilesFacadeError("Host import directory name is invalid", code="invalid_resource_request")
        if collision not in {"fail", "reuse"}:
            raise FilesFacadeError("Host directory collision policy is invalid", code="unsupported_operation")
        scope = self._scope(context)
        if not self._path_writable(context, parent, scope):
            raise FilesFacadeError("Host import directory is read-only", code="resource_unavailable")
        target = os.path.join(parent, folder)
        operation = str(operation_id or f"host-mkdir-{uuid.uuid4().hex}")
        digest = str(request_digest or "")
        loaded = self._directory_operation_load(context, operation)
        if loaded:
            if loaded.get("request_kind") != "create-directory" or loaded.get("request_digest") != digest:
                raise FilesFacadeError("Host directory operation conflicts with an earlier request", code="idempotency_conflict")
            status = await self._directory_status(context, loaded)
            if status and status.get("state") == "complete":
                return await self._directory_resource(context, loaded)
            raise FilesFacadeError("Host directory operation is still pending", code="operation_pending")
        parent_entry = await self.stat(context, origin_id=parent_origin_id)
        if parent_entry.kind != "folder" or "children" not in parent_entry.capabilities or "write" not in parent_entry.capabilities:
            raise FilesFacadeError("Host directory parent is unavailable", code="resource_unavailable")
        if parent_revision is not None and dict(parent_entry.revision or {}) != dict(parent_revision):
            raise FilesFacadeError("Host directory parent is stale", code="resource_changed")
        before_kind = ""
        before_revision = None
        try:
            before = await self.stat(context, origin_id=_origin(target))
            before_kind = str(before.kind)
            before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
        except FilesFacadeError as exc:
            if exc.code != "resource_unavailable":
                raise
        if before_kind:
            if collision == "reuse" and before_kind == "folder":
                marker = {
                    "request_kind": "create-directory", "operation_id": operation,
                    "item_id": operation, "generation": int(context.policy_generation),
                    "workspace_id": str(context.workspace_id or "default"), "phase": "complete",
                    "parent": parent, "target": target, "name": folder,
                    "request_digest": digest, "collision": collision,
                    "parent_revision": dict(parent_revision) if parent_revision is not None else None,
                    "target_before_kind": before_kind, "target_before_revision": before_revision,
                    "outcome": "unchanged",
                }
                self._directory_operation_save(context, operation, digest, marker)
                return await self._directory_resource(context, marker)
            raise FilesFacadeError("Host directory destination already exists", code="resource_changed")
        ownership_path = self._ownership_sidecar(
            parent, operation, folder, digest,
            owner_id=str(context.owner_subject_id),
            workspace_id=str(context.workspace_id or "default"),
        )
        try:
            ownership_path, ownership_token = self._claim_ownership(ownership_path)
        except FileExistsError as exc:
            raise FilesFacadeError("Host directory operation already owns this target", code="resource_changed") from exc
        marker = {
            "request_kind": "create-directory", "operation_id": operation,
            "item_id": operation, "generation": int(context.policy_generation),
            "workspace_id": str(context.workspace_id or "default"), "phase": "pending",
            "parent": parent, "target": target, "name": folder,
            "request_digest": digest, "collision": collision,
            "parent_revision": dict(parent_revision) if parent_revision is not None else None,
            "target_before_kind": before_kind, "target_before_revision": before_revision,
            "ownership_path": ownership_path, "ownership_token": ownership_token,
        }
        phase = "save_marker"
        try:
            self._directory_operation_save(context, operation, digest, marker)
            phase = "mkdir"
            await self._client(context, scope).request("mkdir", target, {})
            phase = "post_mkdir_stat"
            created = await self.stat(context, origin_id=_origin(target))
            if created.kind != "folder" or created.parent_origin_id != parent_origin_id:
                raise FilesFacadeError("Host provider returned an invalid directory", code="provider_unavailable")
            complete = {**marker, "phase": "complete", "revision": dict(created.revision or {})}
            self._directory_operation_save(context, operation, digest, complete)
            self._release_ownership(marker)
            return await self._directory_resource(context, complete)
        except FilesServiceError as exc:
            code = exc.code if exc.code in {"root_unavailable", "denied", "invalid_path", "conflict",
                "deadline_exceeded", "cancelled", "backpressure", "policy_generation_changed", "unauthorized"} else "other"
            recovery = "preimage_unavailable" if str(exc) == "required recovery preimage unavailable; original preserved" else "unknown"
            _LOG.warning("Host directory creation failed phase=%s code=%s recovery=%s", phase, code, recovery)
            if exc.code == "conflict" and collision == "reuse":
                try:
                    existing = await self.stat(context, origin_id=_origin(target))
                except Exception:
                    existing = None
                if existing is not None and existing.kind == "folder" and existing.parent_origin_id == parent_origin_id:
                    complete = {**marker, "phase": "complete", "outcome": "unchanged", "revision": dict(existing.revision or {})}
                    self._directory_operation_save(context, operation, digest, complete)
                    self._release_ownership(marker)
                    return await self._directory_resource(context, complete)
            if exc.code != "conflict":
                # The mkdir response may have been lost after the service
                # committed. Keep ownership and the pending marker so a
                # restarted provider can prove and promote the exact target.
                raise _service_error(exc) from exc
            failed = {**marker, "phase": "failed"}
            try:
                self._directory_operation_save(context, operation, digest, failed)
            finally:
                self._release_ownership(marker)
            raise _service_error(exc) from exc
        except FilesFacadeError as exc:
            code = exc.code if exc.code in {"provider_unavailable", "resource_unavailable", "resource_changed",
                "resource_ref_stale", "operation_pending"} else "other"
            _LOG.warning("Host directory creation failed phase=%s code=%s recovery=unknown", phase, code)
            # A committed target with a lost post-mkdir stat/receipt must stay
            # recoverable through the pending marker. Known validation errors
            # are still surfaced; operation_status will either reconcile the
            # exact folder or leave the operation pending for a later retry.
            raise

    async def roots(self, context: ProviderContext):
        # The visible anchors are children so exact-file grants and Home coexist
        # under one stable provider root, like every other provider namespace.
        self._scope(context)
        return [ProviderResource(
            "host:v1:root",
            "Host locations",
            "provider_root",
            ("children", "stat"),
            provenance={"domain": "host"},
            child_sort_keys=self._ROOT_SORTS,
        )]

    async def children(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        cursor: str | None,
        snapshot: str | None,
        limit: int,
        sort: Mapping[str, Any],
        query: str,
    ) -> ProviderPage:
        scope = self._scope(context)
        if parent_origin_id == "host:v1:root":
            try:
                projection = project_navigation_roots(self.registry, context.owner_username, scope)
            except FilesystemRegistryError as exc:
                raise FilesFacadeError("Host policy is unavailable", code="provider_unavailable") from exc
            by_path: dict[str, dict[str, Any]] = {str(item["path"]): item for item in projection["roots"]}
            default_path = str(projection.get("default_path") or "")
            if default_path:
                by_path.setdefault(default_path, {
                    "path": default_path,
                    "name": "Home" if scope.get("host") else Path(default_path).name or default_path,
                    "kind": "recursive_directory",
                    "capabilities": ["read", "write"] if scope.get("host") else ["read"],
                })
            all_rows = tuple(
                self._resource(
                    str(item["path"]),
                    name=str(item.get("name") or Path(str(item["path"])).name or item["path"]),
                    kind=str(item.get("kind") or "recursive_directory"),
                    provenance={
                        "domain": "host",
                        "favorite": str(item["path"]) == default_path,
                    },
                    writable=self._path_writable(context, str(item["path"]), scope),
                )
                for item in sorted(by_path.values(), key=lambda item: (
                    str(item.get("path")) != default_path,
                    str(item.get("kind")) == "exact_file",
                    str(item.get("name") or "").casefold(),
                ))
            )
            if query:
                folded = query.casefold()
                all_rows = tuple(row for row in all_rows if folded in row.name.casefold())
            value = (
                (lambda row: str(row.sort_kind or row.kind).casefold())
                if sort["key"] == "kind"
                else (lambda row: row.name.casefold())
            )
            ordered_rows = list(all_rows)
            ordered_rows.sort(key=lambda row: row.origin_id)
            ordered_rows.sort(key=value, reverse=sort["direction"] == "desc")
            if bool(sort.get("directories_first", True)):
                ordered_rows.sort(key=lambda row: row.kind != "folder")
            all_rows = tuple(ordered_rows)
            root_snapshot = f"host-roots-{int(scope.get('generation') or 0)}"
            if snapshot is not None and snapshot != root_snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            try:
                offset = int(cursor or 0)
            except (TypeError, ValueError) as exc:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor") from exc
            if offset < 0 or offset > len(all_rows):
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            rows = all_rows[offset:offset + limit]
            next_offset = offset + len(rows)
            return ProviderPage(
                rows,
                next_cursor=str(next_offset) if next_offset < len(all_rows) else None,
                total=len(all_rows),
                snapshot=root_snapshot,
            )

        path = _path(parent_origin_id)
        if query:
            if cursor or snapshot:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor")
            client = self._client(context, scope)
            try:
                response = await client.request("filename_search", path, {
                    "query": query,
                    "max_results": limit,
                    "max_entries": 10_000,
                    "max_depth": 32,
                    "max_bytes_per_file": 0,
                    "include_hidden": False,
                    "case_sensitive": False,
                })
                search_data = response.get("data") or {}
                matches = tuple(str(item) for item in search_data.get("matches") or ())
                search_complete = search_data.get("complete")
                if not isinstance(search_complete, bool):
                    search_complete = None
                rows: list[ProviderResource] = []
                for match in matches:
                    metadata = await client.request("stat", match, {"include_fingerprint": False})
                    data = metadata.get("data") or {}
                    canonical = str(data.get("path") or match)
                    rows.append(self._resource(
                        canonical,
                        name=Path(canonical).name or canonical,
                        kind=str(data.get("kind") or "other"),
                        size=int(data.get("size") or 0),
                        modified_unix_ms=int(data["modified_unix_ms"]) if data.get("modified_unix_ms") is not None else None,
                        writable=self._path_writable(context, canonical, scope),
                        native_available=bool(metadata.get("native_thumbnails")),
                        native_icon_available=bool(metadata.get("native_icons")),
                    ))
            except FilesServiceError as exc:
                raise _service_error(exc) from exc

            key = str(sort.get("key") or "name")
            descending = str(sort.get("direction") or "asc") == "desc"
            directories_first = bool(sort.get("directories_first", True))
            value = {
                "name": lambda row: row.name.casefold(),
                "kind": lambda row: (row.kind.casefold(), row.name.casefold()),
                "size": lambda row: (int(row.size or 0), row.name.casefold()),
                "modified": lambda row: (int(row.modified_unix_ms or 0), row.name.casefold()),
            }[key]
            rows.sort(key=lambda row: row.origin_id)
            rows.sort(key=value, reverse=descending)
            if directories_first:
                rows.sort(key=lambda row: row.kind != "folder")
            digest = hashlib.sha256()
            digest.update(query.encode("utf-8"))
            for row in rows:
                digest.update(f"\0{row.origin_id}\0{row.kind}\0{row.size}\0{row.modified_unix_ms}".encode("utf-8"))
            return ProviderPage(
                tuple(rows),
                next_cursor=None,
                total=len(rows),
                snapshot=f"host-search-{digest.hexdigest()[:24]}",
                complete=search_complete,
            )

        payload: dict[str, Any] = {
            "sort": {
                "key": sort["key"],
                "direction": sort["direction"],
                "directories_first": bool(sort.get("directories_first", True)),
                "collation": "open-clank-v1",
            },
            "limit": limit,
        }
        if cursor:
            try:
                payload["cursor"] = json.loads(cursor)
            except json.JSONDecodeError as exc:
                raise FilesFacadeError("provider cursor is stale", code="stale_cursor") from exc
        try:
            response = await self._client(context, scope).request("list_directory", path, payload)
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        canonical = str(data.get("path") or path)
        entries = tuple(
            self._resource(
                os.path.join(canonical, str(row.get("name") or "")),
                name=str(row.get("name") or ""),
                kind=str(row.get("kind") or "other"),
                size=int(row.get("size") or 0),
                modified_unix_ms=int(row["modified_unix_ms"]) if row.get("modified_unix_ms") is not None else None,
                writable=self._path_writable(context, os.path.join(canonical, str(row.get("name") or "")), scope),
                native_available=bool(response.get("native_thumbnails")),
                native_icon_available=bool(response.get("native_icons")),
            )
            for row in data.get("entries") or ()
            if str(row.get("name") or "")
        )
        next_cursor = data.get("next_cursor")
        return ProviderPage(
            entries,
            next_cursor=json.dumps(next_cursor, separators=(",", ":"), sort_keys=True) if next_cursor else None,
            total=None,
            snapshot=str(data.get("generation") or snapshot or ""),
        )

    async def stat(self, context: ProviderContext, *, origin_id: str) -> ProviderResource:
        if origin_id == "host:v1:root":
            self._scope(context)
            return ProviderResource(
                origin_id,
                "Host locations",
                "provider_root",
                ("children", "stat"),
                provenance={"domain": "host"},
                child_sort_keys=self._ROOT_SORTS,
            )
        path = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request("stat", path, {"include_fingerprint": True})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        canonical = str(data.get("path") or path)
        fingerprint = self._fingerprint_value(data)
        return self._resource(
            canonical,
            name=Path(canonical).name or canonical,
            kind=str(data.get("kind") or "other"),
            size=int(data.get("size") or 0),
            modified_unix_ms=int(data["modified_unix_ms"]) if data.get("modified_unix_ms") is not None else None,
            writable=self._path_writable(context, canonical, scope),
            revision={"kind": "hostFingerprint", "value": fingerprint} if fingerprint else None,
            native_available=bool(response.get("native_thumbnails")),
            native_icon_available=bool(response.get("native_icons")),
        )

    async def create_resource(
        self,
        context: ProviderContext,
        *,
        parent_origin_id: str,
        name: str,
        text: str,
        action_id: str | None = None,
    ) -> ProviderResource:
        """Create one ordinary Markdown file beneath an authorized Host folder."""
        parent = _path(parent_origin_id)
        filename = str(name or "").strip()
        if (
            not filename or len(filename) > 240 or "\x00" in filename
            or "/" in filename or "\\" in filename
            or filename in {".", ".."} or Path(filename).name != filename
            or Path(filename).suffix.lower() not in {".md", ".markdown"}
        ):
            raise FilesFacadeError("Host template name must be a Markdown filename", code="invalid_resource_request")
        if not isinstance(text, str):
            raise FilesFacadeError("Host template text is invalid", code="invalid_resource_request")
        scope = self._scope(context)
        if not self._path_writable(context, parent, scope):
            raise FilesFacadeError("Host template folder is read-only", code="resource_unavailable")
        target = os.path.join(parent, filename)
        try:
            await self._client(context, scope).request("create", target, {"text": text})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        created = await self.stat(context, origin_id=_origin(target))
        receipt_id = str(action_id or f"host-create-{uuid.uuid4().hex}")
        return replace(created, action_receipt={
            "action_id": receipt_id,
            "status": "complete",
            "durable": True,
            "phase": "complete",
            "receipt": {"action_id": receipt_id, "outcome": "created"},
        })

    async def action(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        action: str,
        args: Mapping[str, Any],
        action_id: str | None = None,
    ) -> ProviderResource:
        if action not in {"rename", "move"}:
            raise FilesFacadeError("Host resource action is unavailable", code="resource_unavailable")
        path = _path(origin_id)
        name = str(args.get("name") or "").strip()
        if (
            not name or "/" in name or "\\" in name
            or Path(name).name != name or name in {".", ".."} or "\x00" in name
        ):
            raise FilesFacadeError("Host resource name is invalid", code="invalid_resource_request")
        scope = self._scope(context)
        try:
            current = await self._client(context, scope).request("stat", path, {"include_fingerprint": True})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        current_data = current.get("data") or {}
        fingerprint = self._fingerprint_value(current_data)
        if not fingerprint:
            raise FilesFacadeError("Host resource revision is unavailable", code="provider_unavailable")
        destination = os.path.join(str(Path(path).parent), name)
        try:
            await self._client(context, scope).request(
                "move",
                path,
                {"destination": destination, "expected_fingerprint": {"algorithm": "sha256", "value": fingerprint}},
            )
        except FilesServiceError as exc:
            if exc.code == "conflict":
                raise FilesFacadeError("Host resource changed or destination already exists", code="resource_changed") from exc
            raise _service_error(exc) from exc
        updated = await self.stat(context, origin_id=_origin(destination))
        receipt_id = str(action_id or f"host-{action}-{uuid.uuid4().hex}")
        return replace(updated, action_receipt={
            "action_id": receipt_id,
            "status": "complete",
            "durable": True,
            "phase": "complete",
            "receipt": {"action_id": receipt_id, "outcome": action},
        })

    async def transfer(
        self,
        context: ProviderContext,
        *,
        source_origin_id: str,
        destination_origin_id: str,
        operation: str,
        collision: str,
        expected_revision: Mapping[str, Any] | None = None,
        item_id: str | None = None,
        operation_id: str | None = None,
    ) -> ProviderResource:
        """Move/copy one authorized host item into an authorized folder.

        Both origins were resolved and capability checked by FilesFacade.  The
        existing Rust/files service remains the mutation authority and receives
        server-derived paths only here, after that check.
        """
        if operation not in {"move", "copy"}:
            raise FilesFacadeError("Host transfer operation is unavailable", code="unsupported_operation")
        source_path = _path(source_origin_id)
        destination_path = _path(destination_origin_id)
        scope = self._scope(context)
        try:
            current = await self._client(context, scope).request("stat", source_path, {"include_fingerprint": True})
            source_data = current.get("data") or {}
            fingerprint = self._fingerprint_value(source_data)
            if expected_revision and fingerprint and str(expected_revision.get("value") or "") != fingerprint:
                raise FilesFacadeError("Host resource revision is stale", code="resource_changed")
            name = Path(source_path).name
            candidate = name
            client = self._client(context, scope)
            for attempt in range(101):
                target = os.path.join(destination_path, candidate)
                # The service transfer contract only accepts destination and
                # fingerprint. Select Keep Both names here, as for imports;
                # every candidate still passes the service's exclusive write.
                candidate_operation = operation_id
                if attempt:
                    candidate_operation = "host-transfer-" + uuid.uuid5(
                        uuid.NAMESPACE_URL, f"{operation_id}:{item_id}:candidate:{attempt}"
                    ).hex
                payload = {
                    "destination": target,
                    "expected_fingerprint": {"algorithm": "sha256", "value": fingerprint} if fingerprint else None,
                    "operation_id": candidate_operation,
                    "item_id": item_id,
                }
                try:
                    response = await client.request(operation, source_path, payload)
                    break
                except FilesServiceError as exc:
                    if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                        raise
                    # A conflict may also mean a stale source fingerprint.
                    # Retry only a real occupied destination, with the same
                    # source revision; never turn a revision failure into rename.
                    checked = await client.request("stat", source_path, {"include_fingerprint": True})
                    checked_fingerprint = self._fingerprint_value(checked.get("data") or {})
                    if fingerprint and checked_fingerprint != fingerprint:
                        raise FilesFacadeError("Host resource revision is stale", code="resource_changed") from exc
                    try:
                        await client.request("stat", target, {})
                    except FilesServiceError:
                        raise FilesFacadeError("Host transfer conflict could not be resolved", code="resource_changed") from exc
                    stem, suffix = os.path.splitext(name)
                    candidate = f"{stem} ({attempt + 2}){suffix}"
        except FilesServiceError as exc:
            if exc.code == "conflict":
                raise FilesFacadeError("Host resource changed or destination already exists", code="resource_changed") from exc
            raise _service_error(exc) from exc
        result_data = response.get("data") or {}
        resolved = str(result_data.get("path") or target)
        updated = await self.stat(context, origin_id=_origin(resolved))
        receipt_id = str(operation_id or f"host-transfer-{uuid.uuid4().hex}")
        selected_name = Path(resolved).name or Path(source_path).name
        revision = dict(updated.revision) if isinstance(updated.revision, Mapping) else None
        provenance = dict(updated.provenance)
        provenance["selected_name"] = selected_name
        return replace(updated, provenance=provenance, revision=revision, action_receipt={
            "receipt_id": receipt_id,
            "status": "complete",
            "phase": "complete",
            "durable": True,
            "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
        })

    async def stage_import(self, context: ProviderContext, *, destination_origin_id: str, name: str, upload: Any, operation_id: str, item_id: str) -> Mapping[str, Any]:
        """Stream bounded chunks into a service-owned opaque stage."""
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240 or any(part in name for part in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise FilesFacadeError("Host import name is invalid", code="invalid_resource_request")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        declared = str(getattr(upload, "content_type", "") or "").strip()
        if len(declared.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in declared):
            raise FilesFacadeError("Host import MIME metadata is invalid", code="invalid_resource_request")
        client = self._client(context, scope)
        begin, chunk_fn, abort = (getattr(client, key, None) for key in ("stage_begin", "stage_chunk", "stage_abort"))
        if not all(callable(fn) for fn in (begin, chunk_fn, abort)):
            raise FilesFacadeError("Host staging transport is unavailable", code="unsupported_provider_kind")
        response = await begin(destination)
        data = response.get("data") if isinstance(response, Mapping) else None
        stage_id = str(data.get("stage_id") or "") if isinstance(data, Mapping) else ""
        try:
            chunk_limit = int(data.get("max_chunk_bytes", 0)) if isinstance(data, Mapping) else 0
            total_limit = int(data.get("max_total_bytes", 0)) if isinstance(data, Mapping) else 0
        except (TypeError, ValueError, OverflowError) as error:
            if stage_id:
                try:
                    await abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging bounds are invalid", code="provider_unavailable") from error
        if not stage_id or len(stage_id.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in stage_id) or chunk_limit < 1 or chunk_limit > 512 * 1024 or total_limit < 1 or total_limit > 64 * 1024 * 1024:
            if stage_id:
                try:
                    await abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        digest = hashlib.sha256()
        offset = 0
        detected = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            while True:
                raw = await upload.read(min(chunk_limit, 512 * 1024))
                if not raw:
                    break
                if not isinstance(raw, (bytes, bytearray)):
                    raise FilesFacadeError("Host import stream is invalid", code="invalid_resource_request")
                part = bytes(raw)
                if len(part) > chunk_limit:
                    raise FilesFacadeError("Host import chunk exceeds the service staging limit", code="upload_too_large")
                if offset + len(part) > total_limit:
                    raise FilesFacadeError("Host import exceeds the service staging limit", code="upload_too_large")
                if offset == 0:
                    detected = _detected_mime(name, part[:64])
                digest.update(part)
                await chunk_fn(stage_id, part, offset=offset)
                offset += len(part)
        except BaseException:
            try:
                await abort(stage_id)
            except Exception:
                pass
            raise
        return {"stage_id": stage_id, "length": offset, "digest": "sha256:" + digest.hexdigest(), "selected_name": name, "declared_mime": declared or None, "detected_mime": detected, "max_total_bytes": total_limit, "_client": client}

    async def abort_import(self, context: ProviderContext, *, stage: Mapping[str, Any]) -> None:
        client = stage.get("_client")
        abort = getattr(client, "stage_abort", None)
        if callable(abort):
            await abort(str(stage.get("stage_id") or ""))

    async def finish_import(self, context: ProviderContext, *, destination_origin_id: str, name: str, collision: str, stage: Mapping[str, Any], operation_id: str, item_id: str, request_digest: str | None = None, destination_revision: Mapping[str, Any] | None = None) -> ProviderResource:
        """Commit a service-owned stage, retrying only destination rename."""
        if collision not in {"fail", "rename"}:
            raise FilesFacadeError("Host import collision policy is invalid", code="unsupported_operation")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        client = stage.get("_client") or self._client(context, scope)
        finish = getattr(client, "stage_finish", None)
        stage_id = str(stage.get("stage_id") or "")
        length = int(stage.get("length", -1))
        digest = str(stage.get("digest") or "")
        if not callable(finish) or not stage_id or length < 0 or not digest.startswith("sha256:"):
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        candidate = str(name)
        for attempt in range(101):
            target = os.path.join(destination, candidate)
            target_before_kind = ""
            target_before_revision = None
            try:
                before = await self.stat(context, origin_id=_origin(target))
                target_before_kind = str(before.kind)
                target_before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
            except FilesFacadeError as exc:
                if exc.code != "resource_unavailable":
                    raise
            ownership_path = self._ownership_sidecar(
                destination, operation_id, candidate, digest,
                owner_id=str(context.owner_subject_id),
                workspace_id=str(getattr(context, "workspace_id", "default") or "default"),
            )
            try:
                ownership_path, ownership_token = self._claim_ownership(ownership_path)
            except FileExistsError as exc:
                raise FilesFacadeError("Host import operation already owns this target", code="resource_changed") from exc
            marker = self._import_marker(
                context, operation_id=operation_id, item_id=item_id, target=target,
                length=length, digest=digest, stage=stage, request_digest=request_digest,
                destination=destination, name=name, collision=collision, selected_name=candidate,
                destination_revision=destination_revision,
                ownership_path=ownership_path, ownership_token=ownership_token,
                target_before_kind=target_before_kind, target_before_revision=target_before_revision,
            )
            # Persist before the external commit.  A fresh provider can then
            # inspect this exact target if the response or completion ledger
            # write is lost after stage_finish.
            try:
                self._operation_save(context, operation_id, digest, marker)
            except BaseException:
                self._release_ownership(marker)
                raise
            try:
                response = await finish(stage_id, target, length=length, digest=digest)
                data = response.get("data") or {}
                resolved = str(data.get("path") or target)
                created = await self.stat(context, origin_id=_origin(resolved))
                selected_name = Path(resolved).name or candidate
                revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
                self._operation_save(
                    context, operation_id, digest,
                    self._import_marker(
                        context, operation_id=operation_id, item_id=item_id,
                        target=resolved, length=length, digest=digest, stage=stage,
                        phase="complete", revision=revision, request_digest=request_digest,
                        destination=destination, name=name, collision=collision, selected_name=selected_name,
                        destination_revision=destination_revision,
                        ownership_path=ownership_path, ownership_token=ownership_token,
                        target_before_kind=target_before_kind, target_before_revision=target_before_revision,
                    ),
                )
                self._release_ownership(marker)
                receipt_id = str(operation_id or f"host-import-{uuid.uuid4().hex}")
                return replace(created, provenance={"domain": "host", "source_digest": digest, "selected_name": selected_name, "declared_mime": stage.get("declared_mime"), "detected_mime": stage.get("detected_mime")}, revision=revision, action_receipt={
                    "receipt_id": receipt_id,
                    "status": "complete",
                    "phase": "complete",
                    "durable": True,
                    "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
                })
            except FilesServiceError as exc:
                if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                    if exc.code == "conflict":
                        failed_marker = {**marker, "phase": "failed"}
                        try:
                            self._operation_save(context, operation_id, digest, failed_marker)
                        finally:
                            self._release_ownership(marker)
                        raise FilesFacadeError("Host import destination already exists", code="resource_changed") from exc
                    self._release_ownership(marker)
                    raise _service_error(exc) from exc
                self._release_ownership(marker)
                stem, suffix = os.path.splitext(str(name))
                candidate = f"{stem} ({attempt + 2}){suffix}"
        raise FilesFacadeError("Host import collision could not be resolved", code="resource_changed")

    async def import_file(
        self,
        context: ProviderContext,
        *,
        destination_origin_id: str,
        name: str,
        collision: str,
        upload: Any,
        operation_id: str,
        item_id: str,
    ) -> ProviderResource | Mapping[str, Any]:
        """Import browser bytes into one already-authorized Host folder.

        UploadFile is consumed in bounded chunks into an owned temporary file.
        The Rust service then copies that staging file into its atomic create
        transaction. This keeps Python memory bounded and avoids the previous
        whole-file ``bytes -> list[int]`` expansion.
        """
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 240 or any(part in name for part in ("/", "\\", "\x00")) or name in {".", ".."}:
            raise FilesFacadeError("Host import name is invalid", code="invalid_resource_request")
        if collision not in {"fail", "rename"}:
            raise FilesFacadeError("Host import collision policy is invalid", code="unsupported_operation")
        destination = _path(destination_origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, destination, scope):
            raise FilesFacadeError("Host import destination is read-only", code="resource_unavailable")
        source_digest = hashlib.sha256()
        declared_mime = str(getattr(upload, "content_type", "") or "").strip()
        if len(declared_mime.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in declared_mime):
            raise FilesFacadeError("Host import MIME metadata is invalid", code="invalid_resource_request")
        detected_mime = mimetypes.guess_type(str(name))[0] or "application/octet-stream"
        client = self._client(context, scope)
        stage_begin = getattr(client, "stage_begin", None)
        stage_chunk = getattr(client, "stage_chunk", None)
        stage_finish = getattr(client, "stage_finish", None)
        stage_abort = getattr(client, "stage_abort", None)
        if not all(callable(fn) for fn in (stage_begin, stage_chunk, stage_finish, stage_abort)):
            raise FilesFacadeError("Host staging transport is unavailable", code="unsupported_provider_kind")
        candidate_stage = await stage_begin(destination)
        stage_data = candidate_stage.get("data") if isinstance(candidate_stage, Mapping) else None
        stage_id = str(stage_data.get("stage_id") or "") if isinstance(stage_data, Mapping) else ""
        try:
            chunk_limit = int(stage_data.get("max_chunk_bytes", 0)) if isinstance(stage_data, Mapping) else 0
            total_limit = int(stage_data.get("max_total_bytes", 0)) if isinstance(stage_data, Mapping) else 0
        except (TypeError, ValueError, OverflowError) as error:
            if stage_id:
                try:
                    await stage_abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging bounds are invalid", code="provider_unavailable") from error
        if not stage_id or len(stage_id.encode("utf-8")) > 256 or any(ord(ch) < 32 for ch in stage_id) or chunk_limit < 1 or chunk_limit > 512 * 1024 or total_limit < 1 or total_limit > 64 * 1024 * 1024:
            if stage_id:
                try:
                    await stage_abort(stage_id)
                except Exception:
                    pass
            raise FilesFacadeError("Host import staging handle is invalid", code="provider_unavailable")
        offset = 0
        try:
            while True:
                chunk = await upload.read(min(chunk_limit, 512 * 1024))
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise FilesFacadeError("Host import stream is invalid", code="invalid_resource_request")
                chunk_bytes = bytes(chunk)
                if len(chunk_bytes) > chunk_limit:
                    raise FilesFacadeError("Host import chunk exceeds the service staging limit", code="upload_too_large")
                if offset + len(chunk_bytes) > total_limit:
                    raise FilesFacadeError("Host import exceeds the service staging limit", code="upload_too_large")
                if offset == 0:
                    detected_mime = _detected_mime(str(name), chunk_bytes[:64])
                source_digest.update(chunk_bytes)
                await stage_chunk(stage_id, chunk_bytes, offset=offset)
                offset += len(chunk_bytes)
        except BaseException:
            try: await stage_abort(stage_id)
            except Exception: pass
            raise
        candidate = str(name)
        for attempt in range(101):
            target = os.path.join(destination, candidate)
            digest = "sha256:" + source_digest.hexdigest()
            target_before_kind = ""
            target_before_revision = None
            try:
                before = await self.stat(context, origin_id=_origin(target))
                target_before_kind = str(before.kind)
                target_before_revision = dict(before.revision) if isinstance(before.revision, Mapping) else None
            except FilesFacadeError as exc:
                if exc.code != "resource_unavailable":
                    raise
            ownership_path = self._ownership_sidecar(
                destination, operation_id, candidate, digest,
                owner_id=str(context.owner_subject_id),
                workspace_id=str(getattr(context, "workspace_id", "default") or "default"),
            )
            try:
                ownership_path, ownership_token = self._claim_ownership(ownership_path)
            except FileExistsError as exc:
                raise FilesFacadeError("Host import operation already owns this target", code="resource_changed") from exc
            marker = self._import_marker(
                context, operation_id=operation_id, item_id=item_id,
                target=target, length=offset, digest=digest,
                stage={"declared_mime": declared_mime or None, "detected_mime": detected_mime},
                collision=collision, selected_name=candidate,
                ownership_path=ownership_path, ownership_token=ownership_token,
                target_before_kind=target_before_kind, target_before_revision=target_before_revision,
            )
            try:
                self._operation_save(context, operation_id, digest, marker)
            except BaseException:
                self._release_ownership(marker)
                raise
            try:
                response = await stage_finish(stage_id, target, length=offset, digest=digest)
                data = response.get("data") or {}
                resolved = str(data.get("path") or target)
                created = await self.stat(context, origin_id=_origin(resolved))
                selected_name = Path(resolved).name or candidate
                revision = dict(created.revision) if isinstance(created.revision, Mapping) else None
                self._operation_save(
                    context, operation_id, digest,
                    self._import_marker(
                        context, operation_id=operation_id, item_id=item_id,
                        target=resolved, length=offset, digest=digest,
                        stage={"declared_mime": declared_mime or None, "detected_mime": detected_mime},
                        phase="complete", revision=revision, collision=collision, selected_name=selected_name,
                        ownership_path=ownership_path, ownership_token=ownership_token,
                        target_before_kind=target_before_kind, target_before_revision=target_before_revision,
                    ),
                )
                self._release_ownership(marker)
                receipt_id = str(operation_id or f"host-import-{uuid.uuid4().hex}")
                return replace(created, provenance={
                        "domain": "host",
                        "source_digest": "sha256:" + source_digest.hexdigest(),
                        "selected_name": selected_name,
                        "declared_mime": declared_mime[:256] if declared_mime else None,
                        "detected_mime": detected_mime,
                }, revision=revision, action_receipt={
                    "receipt_id": receipt_id,
                    "status": "complete",
                    "phase": "complete",
                    "durable": True,
                    "receipt": {"receipt_id": receipt_id, "outcome": "committed", "selected_name": selected_name, "revision": revision},
                })
            except FilesServiceError as exc:
                if exc.code != "conflict" or collision != "rename" or attempt >= 100:
                    if exc.code == "conflict":
                        failed_marker = {**marker, "phase": "failed"}
                        try:
                            self._operation_save(context, operation_id, digest, failed_marker)
                        finally:
                            self._release_ownership(marker)
                        raise FilesFacadeError("Host import destination already exists", code="resource_changed") from exc
                    self._release_ownership(marker)
                    raise _service_error(exc) from exc
                self._release_ownership(marker)
                stem, suffix = os.path.splitext(str(name))
                candidate = f"{stem} ({attempt + 2}){suffix}"
        try:
            await stage_abort(stage_id)
        except Exception:
            pass
        raise FilesFacadeError("Host import collision could not be resolved", code="resource_changed")

    async def resource_for_path(self, context: ProviderContext, *, path: str) -> ProviderResource:
        """Mint provider metadata for a server-resolved Workspace path.

        The absolute path stays inside the provider. ``stat`` re-authorizes it
        through the current Rust App scope before the facade issues an opaque
        ResourceRef.
        """

        return await self.stat(context, origin_id=_origin(path))

    async def workspace_target(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
    ) -> ProviderWorkspaceTarget:
        """Resolve one already-opaque Host ref for the Workspace service.

        The canonical absolute path remains server-internal. A regular file is
        opened inside a Workspace rooted at its parent; a directory becomes the
        Workspace root itself. Exact-file-only principals will be denied by the
        canonical Workspace resolver because parent enumeration is not granted.
        """

        path = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request(
                "stat",
                path,
                {"include_fingerprint": False},
            )
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        canonical = str(data.get("path") or path)
        kind = str(data.get("kind") or "other").strip().lower()
        if kind in {"directory", "recursive_directory", "folder"}:
            return ProviderWorkspaceTarget(
                origin_id=origin_id,
                directory_path=canonical,
                name=Path(canonical).name or "Workspace",
            )
        if kind in {"file", "exact_file"}:
            target = Path(canonical)
            return ProviderWorkspaceTarget(
                origin_id=origin_id,
                directory_path=str(target.parent),
                open_relative=target.name,
                name=target.parent.name or "Workspace",
            )
        raise FilesFacadeError("Host resource cannot become a Workspace", code="resource_unavailable")

    async def search(
        self,
        context: ProviderContext,
        *,
        query: str,
        limit: int,
        sort: Mapping[str, Any],
    ) -> ProviderPage:
        anchors = await self.children(
            context,
            parent_origin_id="host:v1:root",
            cursor=None,
            snapshot=None,
            limit=200,
            sort=sort,
            query="",
        )
        scope = self._scope(context)
        search_anchors = anchors.entries
        if scope.get("host"):
            # An administrator's namespace includes `/`, but selecting All
            # Sources must not silently turn an ordinary search into an eager
            # whole-disk crawl. Search the explicit default/Home anchor; the
            # user can enter `/` and search it deliberately when desired.
            favorites = tuple(row for row in search_anchors if row.provenance.get("favorite"))
            search_anchors = favorites or search_anchors[:1]
        by_origin: dict[str, ProviderResource] = {}
        folded = query.casefold()
        for anchor in search_anchors:
            if anchor.kind != "folder":
                if folded in anchor.name.casefold():
                    by_origin.setdefault(anchor.origin_id, anchor)
                continue
            try:
                page = await self.children(
                    context,
                    parent_origin_id=anchor.origin_id,
                    cursor=None,
                    snapshot=None,
                    limit=limit,
                    sort=sort,
                    query=query,
                )
            except FilesFacadeError as exc:
                if exc.code == "resource_unavailable":
                    continue
                raise
            for entry in page.entries:
                by_origin.setdefault(entry.origin_id, entry)
        rows = list(by_origin.values())
        key = str(sort.get("key") or "name")
        value = {
            "name": lambda row: row.name.casefold(),
            "kind": lambda row: (row.kind.casefold(), row.name.casefold()),
            "size": lambda row: (int(row.size or 0), row.name.casefold()),
            "modified": lambda row: (int(row.modified_unix_ms or 0), row.name.casefold()),
        }[key]
        rows.sort(key=lambda row: row.origin_id)
        rows.sort(key=value, reverse=str(sort.get("direction") or "asc") == "desc")
        if bool(sort.get("directories_first", True)):
            rows.sort(key=lambda row: row.kind != "folder")
        bounded = rows[:limit]
        digest = hashlib.sha256()
        digest.update(query.encode("utf-8"))
        for row in bounded:
            digest.update(f"\0{row.origin_id}\0{row.kind}\0{row.size}\0{row.modified_unix_ms}".encode("utf-8"))
        return ProviderPage(
            tuple(bounded),
            next_cursor="truncated" if len(rows) > limit else None,
            total=len(rows),
            snapshot=f"host-global-search-{digest.hexdigest()[:24]}",
        )

    async def query_base(
        self,
        context: ProviderContext,
        *,
        base_origin_id: str,
        corpus_origin_id: str,
        view_id: str | None = None,
        query: Mapping[str, Any] | None = None,
        page: int = 0,
        page_size: int = 100,
        context_origin_id: str | None = None,
        draft_definition: str | None = None,
    ) -> Mapping[str, Any]:
        """Take one bounded, metadata-only recursive Host snapshot.

        Every directory page is authorized by the Rust-backed ``children``
        call.  Paths are converted to corpus-relative POSIX labels only inside
        this provider, and the facade never receives an absolute host path.
        The page argument is applied once, after deterministic snapshot
        ordering; this avoids Copal's former double-slice/empty-page bug.
        """
        base = await self.stat(context, origin_id=base_origin_id)
        corpus = await self.stat(context, origin_id=corpus_origin_id)
        if base.kind not in {"file", "folder"} or corpus.kind not in {"folder", "provider_root"} or "children" not in corpus.capabilities:
            raise FilesFacadeError("Host Base corpus is unavailable", code="resource_unavailable")
        if context_origin_id is not None:
            context_entry = await self.stat(context, origin_id=context_origin_id)
            if context_entry.kind not in {"file", "folder"} or not {"read", "open", "stat"}.intersection(context_entry.capabilities):
                raise FilesFacadeError("Host Base context is unavailable", code="resource_unavailable")
        page_number = int(page)
        size = int(page_size)
        corpus_path = None if corpus_origin_id == "host:v1:root" else _path(corpus_origin_id)
        pending: list[tuple[str, str, int]] = [(corpus_origin_id, "", 0)]
        collected: list[tuple[str, ProviderResource, str]] = []
        complete = True
        folder_page_counts: dict[str, int] = {}
        total_work = 0
        while pending:
            parent_origin, parent_logical, depth = pending.pop(0)
            if depth > self._BASE_MAX_DEPTH:
                complete = False
                continue
            cursor = None
            snapshot = None
            while True:
                folder_page_counts[parent_origin] = folder_page_counts.get(parent_origin, 0) + 1
                total_work += 1
                if total_work > self._BASE_MAX_ROWS:
                    complete = False
                    pending.clear()
                    break
                result = await self.children(
                    context, parent_origin_id=parent_origin, cursor=cursor,
                    snapshot=snapshot, limit=200,
                    sort={"key": "name", "direction": "asc", "directories_first": True}, query="",
                )
                snapshot = result.snapshot or snapshot
                for entry in result.entries:
                    if len(collected) >= self._BASE_MAX_ROWS:
                        complete = False
                        pending.clear()
                        break
                    if corpus_path is None:
                        logical = entry.name
                    else:
                        try:
                            logical = os.path.relpath(_path(entry.origin_id), corpus_path).replace(os.sep, "/")
                        except FilesFacadeError:
                            # An adapter row without a resolvable provider
                            # origin cannot become a Base row.
                            continue
                    if logical in {"", "."} or logical == ".." or logical.startswith("../"):
                        continue
                    collected.append((logical, entry, entry.origin_id))
                    if entry.kind == "folder":
                        pending.append((entry.origin_id, logical, depth + 1))
                if len(collected) >= self._BASE_MAX_ROWS:
                    complete = False
                    pending.clear()
                    break
                if result.next_cursor is None:
                    break
                if folder_page_counts[parent_origin] >= self._BASE_MAX_PAGES_PER_FOLDER:
                    complete = False
                    break
                cursor = result.next_cursor
            if not complete and not pending:
                break

        collected.sort(key=lambda item: (item[0].casefold(), item[0], item[1].origin_id))
        digest = hashlib.sha256()
        digest.update(str(context.owner_subject_id).encode("utf-8"))
        digest.update(str(context.policy_generation).encode("ascii"))
        digest.update(str(corpus_origin_id).encode("utf-8"))
        for logical, entry, _origin_id in collected:
            digest.update(f"\0{logical}\0{entry.kind}\0{entry.size}\0{entry.modified_unix_ms}".encode("utf-8"))
        snapshot_id = f"host-base-{digest.hexdigest()[:32]}"
        offset = page_number * size
        selected = collected[offset:offset + size]
        rows = [{
            "origin_id": origin_id,
            "logical_path": logical,
            "metadata": {"name": entry.name, "kind": entry.kind, "mime_type": entry.mime_type, "size": entry.size, "modified_unix_ms": entry.modified_unix_ms, "capabilities": list(entry.capabilities)},
            "properties": {}, "relations": [],
        } for logical, entry, origin_id in selected]
        return {
            "snapshot_id": snapshot_id,
            "complete": bool(complete),
            "indexing": not bool(complete),
            "truncated": not bool(complete),
            "status": "complete" if complete else "truncated",
            "progress": {"visited": len(collected), "limit": self._BASE_MAX_ROWS},
            "rows": rows,
            "total": len(collected),
        }

    async def content(self, context: ProviderContext, *, origin_id: str) -> ProviderContent:
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        try:
            opened = await client.open_handle(path)
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        handle = opened.get("handle")
        if not isinstance(handle, dict):
            raise FilesFacadeError("Host content transport is unavailable", code="provider_unavailable")
        size = int(opened.get("size") or 0)
        modified = opened.get("modified_unix_ms")
        object_tag = str(opened.get("object_tag") or "").strip()
        filename = Path(path).name or "download"

        async def stream(start: int, length: int):
            offset = int(start)
            remaining = int(length)
            while remaining > 0:
                window = min(remaining, 250 * 1024 * 1024)
                before = offset
                try:
                    async for chunk in client.stream_read_handle(
                        handle,
                        offset=offset,
                        length=window,
                    ):
                        offset += len(chunk)
                        remaining -= len(chunk)
                        yield chunk
                except FilesServiceError as exc:
                    raise _service_error(exc) from exc
                # A short object stream must not spin another request.
                if offset - before < window:
                    break

        return ProviderContent(
            origin_id=origin_id,
            filename=filename,
            media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
            size=size,
            modified_unix_ms=int(modified) if modified is not None else None,
            etag=object_tag or None,
            stream=stream,
        )

    async def open_resource(self, context: ProviderContext, *, origin_id: str) -> Mapping[str, Any] | None:
        """Read one authorized Host text file into the unified Editor DTO.

        The path remains provider-private. Rust performs the authorization,
        decoding, BOM/newline detection, and fingerprinting through its
        ``read_lines`` operation; the browser receives only the immutable text
        snapshot and an opaque ref for subsequent CAS writes.
        """
        path = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request("read_lines", path, {})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        data = response.get("data") or {}
        text = data.get("text")
        fingerprint = data.get("fingerprint")
        if not isinstance(text, str) or not isinstance(fingerprint, Mapping):
            raise FilesFacadeError("Host text resource is unavailable", code="resource_unavailable")
        fingerprint_value = str(fingerprint.get("value") or "").strip()
        if not fingerprint_value:
            raise FilesFacadeError("Host text resource has no stable fingerprint", code="provider_unavailable")
        name = Path(path).name or "Untitled"
        editable = self._path_writable(context, path, scope)
        representation = self._representation(name, data)
        metadata = self._metadata(data)
        return {
            "name": name,
            "kind": "text",
            "corpus": "host",
            "text": text,
            "representation": representation,
            "encoding": data.get("encoding"),
            "newline": data.get("newline"),
            "bom_bytes": data.get("bom_bytes"),
            "mode": data.get("mode"),
            "language": data.get("language"),
            "properties": {},
            "relations": [],
            "tags": [],
            "read_only": not editable,
            "resource": {
                "key": {
                    "accountId": str(context.owner_subject_id),
                    "workspaceId": "host",
                    "provider": "host",
                    "resourceId": stable_resource_id(
                        owner_subject_id=str(context.owner_subject_id),
                        provider="host",
                        origin_id=origin_id,
                    ),
                },
                "revision": {"kind": "hostFingerprint", "value": fingerprint_value},
                # The provider's explicit classification is authoritative. A
                # suffix is never inspected by Notes itself.
                "representation": representation,
                "locator": {"displayName": name, "locationLabel": name},
                "metadata": metadata,
                "capabilities": {
                    "read": True,
                    "edit": editable,
                    "rename": False,
                    "move": False,
                    "trash": False,
                    "attach": False,
                    "reveal": True,
                },
            },
        }

    async def host_applications(self, context: ProviderContext, *, origin_id: str) -> list[dict[str, str]]:
        """Discover installed apps only after the provider reauthorizes the ref."""
        entry = await self.stat(context, origin_id=origin_id)
        if entry.kind != "file" or "open" not in entry.capabilities:
            raise FilesFacadeError("Host resource cannot be opened", code="resource_unavailable")
        try:
            return await host_apps().discover_async(await self._authorized_path(context, origin_id))
        except HostAppsError as error:
            raise FilesFacadeError(str(error), code=error.code) from error

    async def open_on_host(self, context: ProviderContext, *, origin_id: str, app_id: str) -> Mapping[str, str]:
        """Reauthorize and privately resolve the path for one native launch."""
        entry = await self.stat(context, origin_id=origin_id)
        if entry.kind != "file" or "open" not in entry.capabilities:
            raise FilesFacadeError("Host resource cannot be opened", code="resource_unavailable")
        try:
            return await host_apps().launch_async(await self._authorized_path(context, origin_id), app_id)
        except HostAppsError as error:
            raise FilesFacadeError(str(error), code=error.code) from error

    async def _authorized_path(self, context: ProviderContext, origin_id: str) -> str:
        """Return the canonical path from the current authorized service stat."""
        requested = _path(origin_id)
        scope = self._scope(context)
        try:
            response = await self._client(context, scope).request("stat", requested, {"include_fingerprint": True})
        except FilesServiceError as exc:
            raise _service_error(exc) from exc
        canonical = str((response.get("data") or {}).get("path") or "")
        if not canonical or not Path(canonical).is_absolute() or "\x00" in canonical:
            raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
        return canonical

    async def save_resource(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        expected_revision: Mapping[str, Any],
        text: str,
    ) -> Mapping[str, Any]:
        """CAS-save one Host text snapshot without exposing its path to the browser."""
        path = _path(origin_id)
        scope = self._scope(context)
        if not self._path_writable(context, path, scope):
            raise FilesFacadeError("Host resource is read-only", code="resource_unavailable")
        expected_kind = str(expected_revision.get("kind") or "").strip()
        expected_value = str(expected_revision.get("value") or "").strip()
        if expected_kind != "hostFingerprint" or not expected_value:
            raise FilesFacadeError("Host save revision is invalid", code="invalid_resource_request")
        if not isinstance(text, str):
            raise FilesFacadeError("Host save text is invalid", code="invalid_resource_request")
        client = self._client(context, scope)
        name = Path(path).name or "Untitled"

        async def read_snapshot() -> tuple[dict[str, Any], str]:
            try:
                response = await client.request("read_lines", path, {})
            except FilesServiceError as exc:
                raise _service_error(exc) from exc
            data = response.get("data") or {}
            if not isinstance(data, Mapping):
                raise FilesFacadeError("Host text resource snapshot is unavailable", code="provider_unavailable")
            representation = self._representation(name, data)
            # _snapshot validates both decoded text and a stable fingerprint.
            return dict(data), representation

        current, representation = await read_snapshot()
        current_snapshot = self._snapshot(name, current, representation)
        if current_snapshot["revision"]["value"] != expected_value:
            return {"outcome": "conflict", "remote": current_snapshot}

        # The Rust text-edit operation rejects an empty edit (including
        # ``old == new``). A save of an unchanged editor buffer is still a
        # successful CAS no-op, and normalizing line endings here also avoids
        # rewriting bytes when the editor reports its logical newline form.
        normalize_newlines = lambda value: str(value).replace("\r\n", "\n").replace("\r", "\n")
        if normalize_newlines(current_snapshot["envelope"]["text"]) == normalize_newlines(text):
            return {"outcome": "applied", "revision": current_snapshot["revision"], "snapshot": current_snapshot}

        try:
            expected_fingerprint = {"algorithm": "sha256", "value": expected_value}
            if current_snapshot["envelope"]["text"] == "":
                # An empty match is not a text patch. Replace the complete
                # empty snapshot through the native scoped CAS operation,
                # preserving the decoder's encoding and optional BOM.
                encoding = {"Utf8": "utf-8", "Utf16Le": "utf-16-le", "Utf16Be": "utf-16-be", "Utf32Le": "utf-32-le", "Utf32Be": "utf-32-be"}.get(str(current.get("encoding")), str(current.get("encoding") or ""))
                encodings = {
                    "utf-8": ("utf-8", b"\xef\xbb\xbf"),
                    "utf-16-le": ("utf-16-le", b"\xff\xfe"),
                    "utf-16-be": ("utf-16-be", b"\xfe\xff"),
                    "utf-32-le": ("utf-32-le", b"\xff\xfe\x00\x00"),
                    "utf-32-be": ("utf-32-be", b"\x00\x00\xfe\xff"),
                }
                if encoding not in encodings:
                    raise FilesFacadeError("Host text encoding is unavailable", code="provider_unavailable")
                codec, bom = encodings[encoding]
                encoded = (bom if current.get("bom_bytes") else b"") + text.encode(codec)
                await client.request("replace", path, {"bytes": list(encoded), "expected_fingerprint": expected_fingerprint})
            else:
                await client.request(
                    "patch",
                    path,
                    {
                        "old": current_snapshot["envelope"]["text"],
                        "new": text,
                        "replace_all": True,
                        "expected_fingerprint": expected_fingerprint,
                    },
                )
        except FilesServiceError as exc:
            if exc.code != "conflict":
                raise _service_error(exc) from exc
            latest, latest_representation = await read_snapshot()
            return {"outcome": "conflict", "remote": self._snapshot(name, latest, latest_representation)}

        # Re-read through Rust after the patch. This gives the editor the
        # accepted fingerprint and confirms encoding/BOM/newline metadata from
        # the bytes that were actually written.
        latest, latest_representation = await read_snapshot()
        accepted = self._snapshot(name, latest, latest_representation)
        if normalize_newlines(accepted["envelope"]["text"]) != normalize_newlines(text):
            return {"outcome": "conflict", "remote": accepted}
        return {"outcome": "applied", "revision": accepted["revision"], "snapshot": accepted}

    async def thumbnail(
        self,
        context: ProviderContext,
        *,
        origin_id: str,
        width: int,
        height: int,
        scale: float,
        icon: bool = False,
    ) -> bytes:
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        phase = "open_handle"
        try:
            opened = await client.open_handle(path)
            handle = opened.get("handle")
            if not isinstance(handle, dict):
                raise FilesServiceError("Rust filesystem service returned an invalid handle")
            phase = "thumbnail_rpc"
            return await client.thumbnail_handle(
                handle,
                width=width,
                height=height,
                scale=scale,
                **({"icon": True} if icon else {}),
            )
        except FilesServiceError as exc:
            code = exc.code if type(exc.code) is str and exc.code in {
                "denied", "invalid_path", "policy_generation_changed", "conflict",
                "stale_cursor", "backpressure", "stale_handle", "root_unavailable",
                "unsupported", "protocol_mismatch", "partial_stream", "deadline_exceeded",
                "cancelled", "malformed_request", "history_binding_changed", "unauthorized",
            } else "other"
            _LOG.warning("Host native image failed phase=%s code=%s kind=%s",
                         phase, code, "icon" if icon else "thumbnail")
            raise _service_error(exc) from exc

    async def watch(self, context: ProviderContext, *, origin_id: str):
        if origin_id == "host:v1:root":
            raise FilesFacadeError("Host resource is unavailable", code="resource_unavailable")
        path = _path(origin_id)
        scope = self._scope(context)
        client = self._stream_client(context, scope)
        try:
            async for event in client.watch_path(path):
                yield event
        except FilesServiceError as exc:
            raise _service_error(exc) from exc


__all__ = ["HostFilesProvider"]

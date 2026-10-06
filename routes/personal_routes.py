# routes/personal_routes.py
"""Routes for personal documents management."""
import asyncio
import os
import hashlib
import json
import logging
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Tuple
from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, File, Depends
from src.request_models import DirectoryRequest
from core.constants import BASE_DIR, PERSONAL_DIR, PERSONAL_UPLOADS_DIR
from src.rag_singleton import get_rag_manager
from src.auth_helpers import require_privilege, require_user
from core.middleware import require_admin
from src.upload_handler import secure_filename
from src.upload_limits import PERSONAL_UPLOAD_MAX_BYTES
from core.atomic_io import atomic_write_json
from services.memory.skill_lifecycle import locked

UPLOADS_DIR = PERSONAL_UPLOADS_DIR

logger = logging.getLogger(__name__)


def _personal_upload_dir_for_owner(owner: str | None, *, create: bool = True) -> str:
    """Return the per-owner upload directory used for direct RAG uploads."""
    owner_segment = secure_filename((owner or "local").strip())[:80] or "local"
    upload_dir = os.path.abspath(os.path.join(UPLOADS_DIR, owner_segment))
    base_abs = os.path.abspath(UPLOADS_DIR)
    if os.path.commonpath([upload_dir, base_abs]) != base_abs:
        raise ValueError("Unsafe upload owner path")
    if os.path.lexists(upload_dir) and os.path.islink(upload_dir):
        raise ValueError("Personal upload owner directory cannot be a symlink")
    base_real = os.path.realpath(base_abs)
    parent_real = os.path.realpath(os.path.dirname(upload_dir))
    if os.path.commonpath([parent_real, base_real]) != base_real:
        raise ValueError("Unsafe personal upload root")
    if create:
        os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _unique_personal_upload_path(upload_dir: str, original_name: str | None) -> Tuple[str, str, str]:
    """Build a collision-resistant upload path while preserving a display name."""
    safe_name = secure_filename(os.path.basename(original_name or "upload"))
    if not safe_name or safe_name.startswith("."):
        safe_name = "upload"

    stem, ext = os.path.splitext(safe_name)
    stem = (stem or "upload")[:80]
    filename = f"{stem}-{uuid.uuid4().hex[:10]}{ext.lower()}"
    file_path = os.path.abspath(os.path.join(upload_dir, filename))
    upload_abs = os.path.abspath(upload_dir)
    if os.path.commonpath([file_path, upload_abs]) != upload_abs:
        raise ValueError("Unsafe upload filename")
    return file_path, filename, safe_name


def _write_personal_upload(path: str, content: bytes) -> None:
    """Create one direct upload without following or replacing filesystem state."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


class PersonalRagLifecycleError(RuntimeError):
    """A direct-upload/RAG lifecycle transition could not be proven safe."""


class PersonalRagLifecycle:
    """Crash-reconcilable owner lifecycle for direct personal RAG uploads.

    The persisted journal contains the private path map needed to resume a
    filesystem transition.  Returned receipts contain only an opaque token,
    counts, and digests; no source text or local filesystem path is exposed.
    """

    def __init__(
        self,
        *,
        upload_root: str,
        personal_docs_manager: Any = None,
        rag_manager: Any = None,
        journal_path: str | None = None,
        canonical_rag_managed_externally: bool = False,
    ) -> None:
        self.upload_root = os.path.abspath(upload_root)
        self.personal_docs_manager = personal_docs_manager
        self.rag_manager = rag_manager
        self.canonical_rag_managed_externally = bool(canonical_rag_managed_externally)
        self.journal_path = os.path.abspath(
            journal_path or os.path.join(self.upload_root, ".owner-lifecycle.json")
        )
        self._lock = threading.RLock()
        os.makedirs(self.upload_root, exist_ok=True, mode=0o700)

    @contextmanager
    def _guard(self):
        with self._lock:
            with locked(self.journal_path):
                yield

    @staticmethod
    def _owner(owner: str) -> str:
        key = str(owner or "").strip().lower()
        if not key or "\x00" in key:
            raise PersonalRagLifecycleError("personal RAG lifecycle owner is required")
        return key

    def _owner_dir(self, owner: str) -> str:
        segment = secure_filename(self._owner(owner))[:80] or "local"
        candidate = os.path.abspath(os.path.join(self.upload_root, segment))
        if os.path.commonpath([candidate, self.upload_root]) != self.upload_root:
            raise PersonalRagLifecycleError("personal RAG owner path escaped its root")
        if os.path.lexists(candidate) and os.path.islink(candidate):
            raise PersonalRagLifecycleError("personal RAG owner path is a symlink")
        return candidate

    @staticmethod
    def _empty_inventory() -> dict:
        empty_digest = hashlib.sha256(b"").hexdigest()
        return {
            "count": 0,
            "bytes": 0,
            "digest": empty_digest,
            "files": {"count": 0, "bytes": 0, "digest": empty_digest},
            "rag": {
                "available": False,
                "row_count": 0,
                "canonical_row_count": 0,
                "derived_row_count": 0,
                "digest": empty_digest,
                "canonical_semantic_digest": empty_digest,
                "derived_digest": empty_digest,
                "counts": {},
            },
        }

    def _rag_inventory(self, owner: str) -> dict:
        if self.canonical_rag_managed_externally:
            inventory = dict(self._empty_inventory()["rag"])
            inventory["managed_externally"] = True
            return inventory
        inventory = getattr(self.rag_manager, "owner_inventory", None)
        if not callable(inventory):
            return dict(self._empty_inventory()["rag"])
        try:
            result = inventory(owner)
        except Exception as exc:
            raise PersonalRagLifecycleError("personal RAG owner inventory is unavailable") from exc
        if not isinstance(result, dict) or not result.get("available"):
            raise PersonalRagLifecycleError("personal RAG owner inventory is unavailable")
        return dict(result)

    def _file_inventory(self, owner: str) -> dict:
        root = self._owner_dir(owner)
        if not os.path.exists(root):
            return dict(self._empty_inventory()["files"])
        if not os.path.isdir(root):
            raise PersonalRagLifecycleError("personal RAG owner path is not a directory")
        identities: list[str] = []
        total_bytes = 0
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in dirs:
                if os.path.islink(os.path.join(current, name)):
                    raise PersonalRagLifecycleError("personal RAG tree contains a symlink")
            for name in files:
                path = os.path.join(current, name)
                if os.path.islink(path):
                    raise PersonalRagLifecycleError("personal RAG tree contains a symlink")
                relative = os.path.relpath(path, root)
                size = os.path.getsize(path)
                total_bytes += size
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                identities.append(f"{relative}\0{size}\0{digest.hexdigest()}")
        identities.sort()
        return {
            "count": len(identities),
            "bytes": total_bytes,
            "digest": hashlib.sha256("\n".join(identities).encode("utf-8")).hexdigest(),
        }

    def owner_inventory(self, owner: str) -> dict:
        with self._guard():
            files = self._file_inventory(owner)
            rag = self._rag_inventory(owner)
            combined = json.dumps(
                {"files": files.get("digest"), "rag": rag.get("digest")},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                "count": int(files.get("count", 0)) + int(rag.get("row_count", 0)),
                "bytes": int(files.get("bytes", 0)),
                "digest": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
                "files": files,
                "rag": rag,
            }

    @staticmethod
    def _same_inventory(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        return all(left.get(key) == right.get(key) for key in ("count", "bytes", "digest"))

    @classmethod
    def _same_closure(cls, actual: Dict[str, Any], expected: Dict[str, Any]) -> bool:
        actual_files = dict(actual.get("files") or {})
        expected_files = dict(expected.get("files") or {})
        if not all(actual_files.get(key) == expected_files.get(key) for key in ("count", "bytes", "digest")):
            return False
        actual_rag = dict(actual.get("rag") or {})
        expected_rag = dict(expected.get("rag") or {})
        return cls._same_rag_closure(actual_rag, expected_rag)

    @staticmethod
    def _same_rag_closure(actual_rag: Dict[str, Any], expected_rag: Dict[str, Any]) -> bool:
        expected_rows = int(expected_rag.get("row_count", 0))
        if expected_rows and not actual_rag.get("available"):
            raise PersonalRagLifecycleError("nonempty personal RAG state cannot be verified")
        if not expected_rows:
            return int(actual_rag.get("row_count", 0)) == 0
        return (
            int(actual_rag.get("canonical_row_count", 0))
            == int(expected_rag.get("canonical_row_count", 0))
            and str(actual_rag.get("canonical_semantic_digest") or "")
            == str(expected_rag.get("canonical_semantic_digest") or "")
        )

    def _load_journal(self) -> dict:
        try:
            data = json.loads(Path(self.journal_path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "operations": {}}
        except Exception as exc:
            raise PersonalRagLifecycleError("personal RAG lifecycle journal is unreadable") from exc
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("operations"), dict):
            raise PersonalRagLifecycleError("personal RAG lifecycle journal is malformed")
        return data

    def _save_journal(self, journal: dict) -> None:
        os.makedirs(os.path.dirname(self.journal_path), exist_ok=True, mode=0o700)
        atomic_write_json(self.journal_path, journal, indent=2)

    def preview_owner_rename(self, source_owner: str, target_owner: str) -> dict:
        source = self._owner(source_owner)
        target = self._owner(target_owner)
        if source == target:
            raise PersonalRagLifecycleError("source and target owners must differ")
        with self._guard():
            source_inventory = self.owner_inventory(source)
            target_inventory = self.owner_inventory(target)
            if target_inventory["count"] or os.path.exists(self._owner_dir(target)):
                raise PersonalRagLifecycleError("target personal RAG owner already exists")
            token = uuid.uuid4().hex
        return {"version": 1, "path_map_token": token, "source": source_inventory, "target": target_inventory}

    def reconcile_owner_rename(self, source_owner: str, target_owner: str, manifest: dict) -> dict:
        source = self._owner(source_owner)
        target = self._owner(target_owner)
        token = str(manifest.get("path_map_token") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise PersonalRagLifecycleError("invalid personal RAG path-map token")
        with self._guard():
            journal = self._load_journal()
            operation = journal["operations"].get(token)
            if operation is None:
                current_source = self.owner_inventory(source)
                current_target = self.owner_inventory(target)
                if not self._same_inventory(current_source, dict(manifest.get("source") or {})):
                    raise PersonalRagLifecycleError("personal RAG source changed after preview")
                if current_target.get("count") or os.path.exists(self._owner_dir(target)):
                    raise PersonalRagLifecycleError("target personal RAG owner changed after preview")
                operation = {
                    "kind": "rename",
                    "source_owner": source,
                    "target_owner": target,
                    "source_dir": self._owner_dir(source),
                    "target_dir": self._owner_dir(target),
                    "inventory": current_source,
                    "state": "prepared",
                }
                journal["operations"][token] = operation
                self._save_journal(journal)
            elif not isinstance(operation, dict) or operation.get("kind") != "rename":
                raise PersonalRagLifecycleError("personal RAG path-map token is not a rename")
            if operation.get("source_owner") != source or operation.get("target_owner") != target:
                raise PersonalRagLifecycleError("personal RAG path-map token owner mismatch")
            if operation.get("state") == "complete":
                verified = self.verify(source, target, manifest, expected="staged")
                return {"path_map_token": token, **verified}
            source_dir = self._owner_dir(source)
            target_dir = self._owner_dir(target)
            source_exists = os.path.exists(source_dir)
            target_exists = os.path.exists(target_dir)
            if source_exists and target_exists:
                raise PersonalRagLifecycleError("source and target personal RAG directories both exist")
            frozen = dict(operation.get("inventory") or {})
            expected_rag = dict(frozen.get("rag") or {})
            rename_rag = (
                None
                if self.canonical_rag_managed_externally
                else getattr(self.rag_manager, "rename_owner", None)
            )
            if int(expected_rag.get("row_count", 0)) and not callable(rename_rag):
                raise PersonalRagLifecycleError("nonempty personal RAG state cannot be renamed")
            if source_exists:
                if not self._same_inventory(self.owner_inventory(source), frozen):
                    raise PersonalRagLifecycleError("personal RAG source changed after preview")
                os.replace(source_dir, target_dir)
            elif not target_exists:
                if int(frozen.get("count", -1)) != 0:
                    raise PersonalRagLifecycleError("personal RAG source and target are both missing")
            path_map = {}
            for current, _dirs, files in os.walk(target_dir, followlinks=False):
                for name in files:
                    target_path = os.path.join(current, name)
                    relative = os.path.relpath(target_path, target_dir)
                    path_map[os.path.join(source_dir, relative)] = target_path
            operation["path_map"] = path_map
            operation["state"] = "filesystem_staged"
            self._save_journal(journal)

            rename_directory = getattr(self.personal_docs_manager, "rename_directory", None)
            if callable(rename_directory):
                rename_directory(source_dir, target_dir, path_map=path_map)
            path_receipt = None
            if self.canonical_rag_managed_externally:
                rewrite_paths = getattr(self.rag_manager, "rewrite_owner_paths", None)
                if callable(rewrite_paths):
                    path_receipt = rewrite_paths(
                        target,
                        path_map=path_map,
                        path_prefixes=[(source_dir, target_dir)],
                    )
                elif getattr(self.rag_manager, "backend", None) == "frankenmemory":
                    raise PersonalRagLifecycleError(
                        "canonical RAG source paths cannot be reconciled"
                    )
            elif callable(rename_rag):
                source_rag = self._rag_inventory(source)
                target_rag = self._rag_inventory(target)
                already_staged = (
                    int(source_rag.get("row_count", 0)) == 0
                    and self._same_rag_closure(target_rag, expected_rag)
                )
                if not already_staged:
                    if int(target_rag.get("row_count", 0)):
                        raise PersonalRagLifecycleError("source and target personal RAG owners both contain state")
                    rename_rag(source, target, path_map=path_map, path_prefixes=[(source_dir, target_dir)])
            if not self._same_closure(self.owner_inventory(target), frozen):
                raise PersonalRagLifecycleError("personal RAG staged inventory does not match preview")
            operation["state"] = "complete"
            operation["path_rows_rewritten"] = int(
                (path_receipt or {}).get("updated_count", 0)
            )
            operation.pop("path_map", None)
            operation.pop("source_dir", None)
            operation.pop("target_dir", None)
            self._save_journal(journal)
        return {
            "state": "staged",
            "path_map_token": token,
            "source": self.owner_inventory(source),
            "target": self.owner_inventory(target),
        }

    def rename_owner(self, source_owner: str, target_owner: str, manifest: dict | None = None) -> dict:
        frozen = manifest or self.preview_owner_rename(source_owner, target_owner)
        return self.reconcile_owner_rename(source_owner, target_owner, frozen)

    def stage_to_tombstone(self, source_owner: str, tombstone_owner: str, manifest: dict) -> dict:
        return self.reconcile_owner_rename(source_owner, tombstone_owner, manifest)

    def compensate(self, source_owner: str, target_owner: str, manifest: dict) -> dict:
        with self._guard():
            source = self._owner(source_owner)
            target = self._owner(target_owner)
            frozen = dict(manifest.get("source") or {})
            source_inventory = self.owner_inventory(source)
            target_inventory = self.owner_inventory(target)
            restored = self._same_closure(source_inventory, frozen)
            if restored and target_inventory["count"]:
                raise PersonalRagLifecycleError("source and target personal RAG owners both exist")

            forward_token = str(manifest.get("path_map_token") or "")
            journal = self._load_journal()
            forward_operation = journal["operations"].get(forward_token)
            reverse = (
                dict(forward_operation.get("compensation_manifest") or {})
                if isinstance(forward_operation, dict)
                else {}
            )

            if restored and not reverse:
                # A previous process may have moved the bytes back before it
                # could persist or finish the external canonical-RAG path
                # rewrite.  File inventory alone cannot prove those paths are
                # current because account mode deliberately leaves canonical
                # RAG ownership to the Memory lifecycle.  Re-run the metadata
                # transition explicitly; both adapters are idempotent.
                if self.canonical_rag_managed_externally:
                    source_dir = self._owner_dir(source)
                    target_dir = self._owner_dir(target)
                    path_map = {}
                    for current, _dirs, files in os.walk(source_dir, followlinks=False):
                        for name in files:
                            source_path = os.path.join(current, name)
                            relative = os.path.relpath(source_path, source_dir)
                            path_map[os.path.join(target_dir, relative)] = source_path
                    rename_directory = getattr(
                        self.personal_docs_manager,
                        "rename_directory",
                        None,
                    )
                    if callable(rename_directory):
                        rename_directory(target_dir, source_dir, path_map=path_map)
                    rewrite_paths = getattr(self.rag_manager, "rewrite_owner_paths", None)
                    if callable(rewrite_paths):
                        rewrite_paths(
                            source,
                            path_map=path_map,
                            path_prefixes=[(target_dir, source_dir)],
                        )
                    elif getattr(self.rag_manager, "backend", None) == "frankenmemory":
                        raise PersonalRagLifecycleError(
                            "canonical RAG source paths cannot be reconciled"
                        )
                return {
                    "state": "restored",
                    "source": self.owner_inventory(source),
                    "target": self.owner_inventory(target),
                }

            if not reverse:
                reverse = self.preview_owner_rename(target, source)
                # Persist the reverse token before moving anything.  If the
                # path rewrite fails after os.replace(), the next compensation
                # call resumes this exact journaled transition instead of
                # mistaking restored bytes for a fully restored closure.
                journal = self._load_journal()
                forward_operation = journal["operations"].get(forward_token)
                if not isinstance(forward_operation, dict):
                    raise PersonalRagLifecycleError(
                        "personal RAG compensation source operation is missing"
                    )
                forward_operation["compensation_manifest"] = reverse
                self._save_journal(journal)

            restored_receipt = self.reconcile_owner_rename(target, source, reverse)
            if not self._same_inventory(restored_receipt["target"], frozen):
                raise PersonalRagLifecycleError("personal RAG compensation verification failed")
            return {
                "state": "restored",
                "source": restored_receipt["target"],
                "target": restored_receipt["source"],
            }

    def verify(self, source_owner: str, target_owner: str, manifest: dict, *, expected: str = "staged") -> dict:
        with self._guard():
            source = self.owner_inventory(source_owner)
            target = self.owner_inventory(target_owner)
            closure = dict(manifest.get("source") or {})
            valid = (
                source.get("count") == 0 and self._same_closure(target, closure)
                if expected == "staged"
                else self._same_closure(source, closure) and target.get("count") == 0
                if expected == "restored"
                else False
            )
            if not valid:
                raise PersonalRagLifecycleError(f"personal RAG lifecycle did not reach {expected}")
            return {"state": expected, "source": source, "target": target}

    def preview_owner_purge(self, owner: str) -> dict:
        return self.owner_inventory(owner)

    def purge_owner(self, owner: str, *, expected: dict | None = None, operation_token: str | None = None) -> dict:
        owner_key = self._owner(owner)
        token = operation_token or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", token):
            raise PersonalRagLifecycleError("invalid personal RAG purge token")
        with self._guard():
            journal = self._load_journal()
            operation = journal["operations"].get(token)
            owner_dir = self._owner_dir(owner_key)
            quarantine = os.path.join(self.upload_root, ".owner-lifecycle-bytes", token)
            if operation is None:
                before = self.owner_inventory(owner_key)
                if expected is not None and not self._same_inventory(before, expected):
                    raise PersonalRagLifecycleError("personal RAG purge inventory changed after preview")
                if os.path.lexists(quarantine):
                    raise PersonalRagLifecycleError("personal RAG purge target already exists")
                operation = {
                    "kind": "purge", "owner": owner_key, "source_dir": owner_dir,
                    "target_dir": quarantine, "inventory": before, "state": "prepared",
                }
                journal["operations"][token] = operation
                self._save_journal(journal)
            elif operation.get("kind") != "purge" or operation.get("owner") != owner_key:
                raise PersonalRagLifecycleError("personal RAG purge token owner mismatch")
            elif operation.get("state") == "complete":
                after = self.owner_inventory(owner_key)
                if after["count"]:
                    raise PersonalRagLifecycleError("completed personal RAG purge has owner bytes")
                return {
                    "state": "purged", "path_map_token": token,
                    "before": dict(operation.get("inventory") or {}), "after": after,
                    "rag_changed": False,
                }

            expected_rag = dict((operation.get("inventory") or {}).get("rag") or {})
            purge_rag = (
                None
                if self.canonical_rag_managed_externally
                else getattr(self.rag_manager, "purge_owner", None)
            )
            if int(expected_rag.get("row_count", 0)) and not callable(purge_rag):
                raise PersonalRagLifecycleError("nonempty personal RAG state cannot be purged")
            source_exists = os.path.exists(owner_dir)
            target_exists = os.path.exists(quarantine)
            if source_exists and target_exists:
                raise PersonalRagLifecycleError("personal RAG purge source and target both exist")
            if source_exists:
                self.owner_inventory(owner_key)  # validates the full tree, including symlinks
                os.makedirs(os.path.dirname(quarantine), exist_ok=True, mode=0o700)
                os.replace(owner_dir, quarantine)
            operation["state"] = "bytes_staged"
            self._save_journal(journal)

            rag_receipt = purge_rag(owner_key) if callable(purge_rag) else None
            remove_directory = getattr(self.personal_docs_manager, "remove_directory", None)
            if callable(remove_directory):
                if self.canonical_rag_managed_externally:
                    remove_directory(owner_dir, owner=owner_key, remove_rag=False)
                else:
                    remove_directory(owner_dir, owner=owner_key)
            operation["state"] = "metadata_removed"
            self._save_journal(journal)
            if os.path.isdir(quarantine):
                shutil.rmtree(quarantine)
            operation["state"] = "complete"
            operation.pop("source_dir", None)
            operation.pop("target_dir", None)
            self._save_journal(journal)
        after = self.owner_inventory(owner_key)
        if after["count"]:
            raise PersonalRagLifecycleError("personal RAG purge verification failed")
        return {
            "state": "purged", "path_map_token": token,
            "before": dict(operation.get("inventory") or {}), "after": after,
            "rag_changed": bool(rag_receipt),
        }


def _unique_existing_target(path: str) -> str:
    """Return a non-existing sibling path for rename collision handling."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    while True:
        candidate = f"{stem}-{uuid.uuid4().hex[:10]}{ext}"
        if not os.path.exists(candidate):
            return candidate


def _remove_empty_tree(path: str) -> None:
    """Best-effort removal of empty directories under ``path``."""
    if not os.path.isdir(path):
        return
    for root, dirs, _files in os.walk(path, topdown=False):
        for dirname in dirs:
            candidate = os.path.join(root, dirname)
            try:
                os.rmdir(candidate)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def _index_personal_upload_batch(
    *,
    pending: list[tuple[UploadFile, bytes]],
    user: str,
    rag: Any,
    personal_docs_manager: Any,
) -> dict:
    """Persist and index one already-read batch under the lifecycle lock."""
    upload_dir = _personal_upload_dir_for_owner(user)
    total_indexed = 0
    total_failed = 0
    uploaded_files: list[str] = []
    lock_target = os.path.join(os.path.abspath(UPLOADS_DIR), ".owner-lifecycle.json")
    with locked(lock_target):
        for upload, content_bytes in pending:
            try:
                file_path, stored_name, safe_name = _unique_personal_upload_path(
                    upload_dir, upload.filename
                )
                _write_personal_upload(file_path, content_bytes)
                ext = os.path.splitext(safe_name)[1].lower()
                if ext == ".pdf":
                    from src.personal_docs import extract_pdf_text

                    text = extract_pdf_text(file_path)
                else:
                    text = content_bytes.decode("utf-8", errors="replace")
                if not text or not text.strip():
                    total_failed += 1
                    continue

                metadata = {
                    "source": file_path,
                    "filename": safe_name,
                    "stored_filename": stored_name,
                    "directory": upload_dir,
                    "type": ext,
                    "owner": user,
                }
                if getattr(rag, "backend", "") == "frankenmemory":
                    if rag.add_document(text, metadata):
                        from src.frankenmemory_rag import _chunks

                        total_indexed += len(list(_chunks(text)))
                    else:
                        total_failed += 1
                else:
                    chunks = rag._split_into_chunks(text, chunk_size=500)
                    for index, chunk in enumerate(chunks):
                        chunk_metadata = {**metadata, "chunk_id": index}
                        if rag.add_document(chunk, chunk_metadata):
                            total_indexed += 1
                        else:
                            total_failed += 1
                uploaded_files.append(safe_name)
            except Exception as exc:
                logger.error("Failed to upload/index %s: %s", upload.filename, exc)
                total_failed += 1

        if uploaded_files and hasattr(personal_docs_manager, "add_directory"):
            personal_docs_manager.add_directory(upload_dir, index=False)
    return {
        "uploaded": uploaded_files,
        "indexed_count": total_indexed,
        "failed_count": total_failed,
    }


def rename_personal_upload_owner(
    old_owner: str,
    new_owner: str,
    *,
    personal_docs_manager: Any = None,
    rag_manager: Any = None,
) -> Dict[str, Any]:
    """Move direct personal uploads and rewrite RAG owner metadata on user rename."""
    old_dir = _personal_upload_dir_for_owner(old_owner, create=False)
    new_dir = _personal_upload_dir_for_owner(new_owner, create=False)
    path_map: Dict[str, str] = {}
    moved_files = 0

    if os.path.isdir(old_dir) and old_dir != new_dir:
        os.makedirs(new_dir, exist_ok=True)
        for root, _dirs, files in os.walk(old_dir):
            rel_root = os.path.relpath(root, old_dir)
            target_root = new_dir if rel_root == "." else os.path.join(new_dir, rel_root)
            os.makedirs(target_root, exist_ok=True)
            for filename in files:
                source = os.path.abspath(os.path.join(root, filename))
                target = _unique_existing_target(os.path.abspath(os.path.join(target_root, filename)))
                shutil.move(source, target)
                path_map[source] = target
                moved_files += 1
        _remove_empty_tree(old_dir)

    if personal_docs_manager is not None:
        rename_directory = getattr(personal_docs_manager, "rename_directory", None)
        if callable(rename_directory):
            rename_directory(old_dir, new_dir, path_map=path_map)

    rag_result = None
    if rag_manager is not None:
        rename_owner = getattr(rag_manager, "rename_owner", None)
        if callable(rename_owner):
            rag_result = rename_owner(
                old_owner,
                new_owner,
                path_map=path_map,
                path_prefixes=[(old_dir, new_dir)],
            )

    return {
        "old_dir": old_dir,
        "new_dir": new_dir,
        "moved_files": moved_files,
        "path_map": path_map,
        "rag_result": rag_result,
    }


def setup_personal_routes(personal_docs_manager, rag_manager, rag_available):
    """
    Setup personal documents related routes.

    Args:
        personal_docs_manager: PersonalDocsManager instance
        rag_manager: RAG manager instance (may be None)
        rag_available: Boolean indicating if RAG is available

    Returns:
        APIRouter instance with personal docs routes
    """
    router = APIRouter(prefix="/api/personal")
    # Each owner has one lifecycle lane for RAG/tracking mutations.  Different
    # accounts can progress independently; one account cannot interleave an
    # add, direct upload, or delete across the unsynchronised RAG index.
    _owner_lifecycle_locks: dict[str, asyncio.Lock] = {}

    def _owner_lifecycle_lock(owner: str | None) -> asyncio.Lock:
        key = str(owner or "local-installation").strip().lower()
        return _owner_lifecycle_locks.setdefault(key, asyncio.Lock())

    def _rag():
        """Get the current RAG manager, retrying init if needed."""
        return get_rag_manager()

    def _resolve_allowed_personal_dir(directory: str) -> str:
        """Resolve a user-supplied personal-docs path under the allowed root."""
        if not directory:
            raise HTTPException(400, "Directory path is required")

        # realpath (not abspath) so a symlink inside PERSONAL_DIR that points
        # outside it is resolved before the commonpath confinement check below;
        # abspath only normalises `..` and would let such a symlink escape.
        base_abs = os.path.realpath(PERSONAL_DIR)
        candidate = directory if os.path.isabs(directory) else os.path.join(base_abs, directory)
        resolved = os.path.realpath(candidate)
        try:
            in_base = os.path.commonpath([resolved, base_abs]) == base_abs
        except ValueError:
            in_base = False
        if not in_base:
            raise HTTPException(403, "Directory must be inside personal documents")
        return resolved
    
    @router.get("")
    def api_personal_list(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Enhanced version that includes directories"""
        files = [{"name": f["name"], "size": f["size"], "path": f.get("path", "")} for f in personal_docs_manager.index]
        directories = personal_docs_manager.get_indexed_directories() if hasattr(personal_docs_manager, "get_indexed_directories") else []
        return {"files": files, "directories": directories}
    
    @router.post("/reload")
    def api_personal_reload(owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        personal_docs_manager.refresh_index()
        return {"ok": True, "count": len(personal_docs_manager.index)}
    
    @router.post("/add_directory")
    async def add_directory_to_rag(
        request: Request,
        directory_request: DirectoryRequest,
        owner: str = Depends(require_user), _admin: None = Depends(require_admin),
    ):
        """
        Add a directory and all its subdirectories/files to the RAG index.
        
        Args:
            directory_request: Directory request model containing the directory path
            
        Returns:
            JSON response with indexing results
        """
        directory = directory_request.directory
        try:
            directory = _resolve_allowed_personal_dir(directory)
            
            # Security check - ensure directory exists and is accessible
            if not os.path.exists(directory):
                raise HTTPException(404, f"Directory not found: {directory}")
            
            if not os.path.isdir(directory):
                raise HTTPException(400, f"Path is not a directory: {directory}")
            
            logger.info(f"Adding directory to RAG: {directory}")
            
            # Use the RAGManager to index the directory
            rag = _rag()
            if rag:
                # Directory indexing walks files and persists the derived RAG
                # state.  Keep that blocking work off the request loop so a
                # large admitted personal tree does not stall chat streams.
                async with _owner_lifecycle_lock(owner):
                    result = await asyncio.to_thread(
                        rag.index_personal_documents, directory, owner=owner
                    )
                    if result["success"]:
                        # Keep tracking in the same owner lifecycle transition as
                        # indexing so a concurrent upload/delete cannot overwrite it.
                        personal_docs_manager.add_directory(directory, index=False)

                if result["success"]:
                    return {
                        "success": True,
                        "message": f"Successfully indexed {result['indexed_count']} chunks from {directory}",
                        "indexed_count": result["indexed_count"],
                        "failed_count": result.get("failed_count", 0),
                        "directory": directory
                    }
                else:
                    raise HTTPException(500, result.get("message", "Failed to index directory"))
            else:
                raise HTTPException(503, "RAG system is not available")
                
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error adding directory to RAG: {e}")
            raise HTTPException(500, f"Failed to add directory: {str(e)}")
    
    @router.delete("/remove_directory")
    async def remove_directory_from_rag(directory: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """
        Remove a directory from the RAG index.

        Args:
            directory: Path to the directory to remove

        Returns:
            JSON response confirming removal
        """
        try:
            # Confine to PERSONAL_DIR — parity with add_directory_to_rag (which
            # resolves the path the same way). Without this, an arbitrary or
            # `..`-escaping path is passed straight to
            # personal_docs_manager.remove_directory / rag.remove_directory.
            directory = _resolve_allowed_personal_dir(directory)

            logger.info(f"Removing directory from RAG: {directory}")

            # Always remove from personal_docs_manager tracking
            if hasattr(personal_docs_manager, 'remove_directory'):
                personal_docs_manager.remove_directory(directory, owner=owner)

            # Remove from RAG vector store (best-effort)
            rag = _rag()
            if rag:
                try:
                    rag.remove_directory(directory, owner=owner)
                except Exception as e:
                    logger.warning(f"RAG removal failed for directory {directory}: {e}")

            return {
                "success": True,
                "message": f"Successfully removed {directory} from RAG index",
                "directory": directory
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error removing directory from RAG: {e}")
            raise HTTPException(500, f"Failed to remove directory: {str(e)}")
    
    @router.post("/upload")
    async def upload_files_to_rag(request: Request, files: List[UploadFile] = File(...)):
        """Upload files directly into RAG. Supports text and PDF."""
        user = require_privilege(request, "can_use_documents")
        rag = _rag()
        if not rag:
            raise HTTPException(503, "RAG system is not available — is the embedding service running?")

        pending: list[tuple[UploadFile, bytes]] = []
        oversized = 0
        for upload in files:
            content_bytes = await upload.read(PERSONAL_UPLOAD_MAX_BYTES + 1)
            if len(content_bytes) > PERSONAL_UPLOAD_MAX_BYTES:
                logger.warning("Rejected oversized personal upload: %r", upload.filename)
                oversized += 1
            else:
                pending.append((upload, content_bytes))

        async with _owner_lifecycle_lock(user):
            result = await asyncio.to_thread(
                _index_personal_upload_batch,
                pending=pending,
                user=user,
                rag=rag,
                personal_docs_manager=personal_docs_manager,
            )

        return {
            "success": True,
            "uploaded": result["uploaded"],
            "indexed_count": result["indexed_count"],
            "failed_count": result["failed_count"] + oversized,
        }

    @router.delete("/file")
    async def delete_file_from_rag(filepath: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Delete a specific file from RAG index and optionally from disk."""
        try:
            async with _owner_lifecycle_lock(owner):
                # Remove chunks from RAG vector store (best-effort)
                removed = 0
                rag = _rag()
                if rag:
                    try:
                        removed = rag.delete_by_source(filepath, owner=owner)
                    except TypeError:
                        # Keep narrow compatibility with older test/extension
                        # doubles whose method predates owner-scoped RAG.  The
                        # canonical Frankenmemory implementation accepts owner;
                        # never use this fallback for it.
                        removed = rag.delete_by_source(filepath)
                    except Exception as e:
                        logger.warning(f"RAG removal failed for {filepath}: {e}")

                # Delete file from disk if it's in the caller's own uploads dir.
                # Scope to the per-owner subdir, not the shared uploads root, so one
                # admin can't delete another user's personal files by path.
                deleted_from_disk = False
                try:
                    abs_target = os.path.realpath(filepath)
                    base_abs = os.path.realpath(_personal_upload_dir_for_owner(owner, create=False))
                    in_uploads = (
                        abs_target == base_abs
                        or os.path.commonpath([abs_target, base_abs]) == base_abs
                    )
                except ValueError:
                    # commonpath raises on mixed drives / non-comparable paths
                    in_uploads = False
                if in_uploads and abs_target != base_abs:
                    try:
                        os.remove(abs_target)
                        deleted_from_disk = True
                    except FileNotFoundError:
                        pass  # already gone — race with another request or cleanup

                # Exclude the file from the listing (persists across restarts)
                personal_docs_manager.exclude_file(filepath)
            return {
                "success": True,
                "removed_chunks": removed,
                "deleted_from_disk": deleted_from_disk,
            }
        except Exception as e:
            logger.error(f"Failed to delete file {filepath}: {e}")
            raise HTTPException(500, f"Failed to delete file: {str(e)}")

    return router

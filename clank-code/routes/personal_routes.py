# routes/personal_routes.py
"""Routes for personal documents management."""
import asyncio
import os
import hashlib
import json
import logging
import math
import re
import shutil
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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

_PERSONAL_DOCUMENT_AUTHORITY_SEAL = object()


@dataclass(frozen=True)
class _PersonalDocumentAuthority:
    """Original Host admission; source evidence is not a write grant."""
    owner: str
    account: str
    snapshot: object
    _check: object
    _seal: object

    def __post_init__(self):
        if self._seal is not _PERSONAL_DOCUMENT_AUTHORITY_SEAL:
            raise ValueError('personal document authority unavailable')

    def current_account(self):
        return self._seal is _PERSONAL_DOCUMENT_AUTHORITY_SEAL and self._check(False) is True

    def current_source(self):
        return self._seal is _PERSONAL_DOCUMENT_AUTHORITY_SEAL and self._check(True) is True


_RELOCATION_PROOF_SEAL = object()


class _NativeRelocationProof:
    def __init__(self, capability, provider, step, receipt, relocation, seal):
        if seal is not _RELOCATION_PROOF_SEAL:
            raise PersonalRagLifecycleError('native relocation proof unavailable')
        self.capability, self.provider, self.step = capability, provider, step
        self.receipt, self.relocation, self._seal = receipt, relocation, seal

    def assert_current(self, source, target, manifest):
        if self._seal is not _RELOCATION_PROOF_SEAL:
            raise PersonalRagLifecycleError('native relocation proof unavailable')
        self.capability.assert_current(provider=self.provider, action='rename', owner=source, new_owner=target)
        operation = self.capability.lifecycle.get_operation(self.capability.operation_id)
        step = (operation.get('steps') or {}).get(self.step) or {}
        manifests = operation.get('manifest') or {}
        base_step = 'memory_fm_tombstoned' if self.step.startswith('memory_fm_reverse_') else self.step
        base = manifests.get(base_step) or {}
        bound = base.get('rag_relocation')
        if self.step.startswith('memory_fm_reverse_') and bound is not None:
            bound = {'from': bound['to'], 'to': bound['from']}
        if (step.get('state') != 'applied' or step.get('receipt') != self.receipt
                or 'rag_relocation' not in base or bound != self.relocation
                or manifest.get('rag_relocation') != self.relocation):
            raise PersonalRagLifecycleError('native relocation proof changed')


def _issue_native_relocation_proof(*, capability, provider, step, receipt, relocation, source, target, manifest):
    from src.openclank.chat_source_bridge import HostAccountSourceTransition
    if type(capability) is not HostAccountSourceTransition:
        raise PersonalRagLifecycleError('native relocation authority unavailable')
    proof = _NativeRelocationProof(capability, provider, step, receipt, relocation, _RELOCATION_PROOF_SEAL)
    proof.assert_current(source, target, manifest)
    return proof


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

    def assert_relocation_current(self, source_owner: str, target_owner: str, manifest: dict) -> None:
        if self.canonical_rag_managed_externally:
            expected = {'from': self._owner_dir(source_owner), 'to': self._owner_dir(target_owner)}
            if type(manifest) is not dict or manifest.get('rag_relocation') != expected:
                raise PersonalRagLifecycleError('personal RAG relocation root changed')

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
            source_dir = os.path.abspath(self._owner_dir(source))
            target_dir = os.path.abspath(self._owner_dir(target))
            # The external FM inventory is intentionally not included in the
            # Personal inventory, so an empty directory cannot prove that no
            # source URI uses this prefix.
            has_source = (self.canonical_rag_managed_externally or os.path.exists(source_dir)
                          or int((source_inventory.get('rag') or {}).get('row_count', 0)) > 0)
            relocation = {'from': source_dir, 'to': target_dir} if has_source else None
        return {"version": 1, "path_map_token": token, "source": source_inventory, "target": target_inventory,
                "rag_relocation": relocation}

    def reconcile_owner_rename(self, source_owner: str, target_owner: str, manifest: dict, *, native_relocation_proof=None) -> dict:
        source = self._owner(source_owner)
        target = self._owner(target_owner)
        if native_relocation_proof is not None:
            self.assert_relocation_current(source, target, manifest)
            if type(native_relocation_proof) is not _NativeRelocationProof:
                raise PersonalRagLifecycleError('native relocation proof unavailable')
            native_relocation_proof.assert_current(source, target, manifest)
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
            if int(expected_rag.get("row_count", 0)) and not callable(rename_rag) and native_relocation_proof is None:
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
            if self.canonical_rag_managed_externally and native_relocation_proof is not None:
                native_relocation_proof.assert_current(source, target, manifest)
            elif self.canonical_rag_managed_externally:
                rewrite_paths = getattr(self.rag_manager, "rewrite_owner_paths", None)
                if callable(rewrite_paths):
                    path_receipt = rewrite_paths(
                        target,
                        path_map=path_map,
                        path_prefixes=[(source_dir, target_dir)],
                    )
                elif getattr(self.rag_manager, "backend", None) == "memery":
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
            if native_relocation_proof is not None:
                native_relocation_proof.assert_current(source, target, manifest)
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

    def stage_to_tombstone(self, source_owner: str, tombstone_owner: str, manifest: dict, *, native_relocation_proof=None) -> dict:
        return self.reconcile_owner_rename(source_owner, tombstone_owner, manifest, native_relocation_proof=native_relocation_proof)

    def compensate(self, source_owner: str, target_owner: str, manifest: dict, *, native_relocation_proof=None) -> dict:
        with self._guard():
            source = self._owner(source_owner)
            target = self._owner(target_owner)
            if native_relocation_proof is not None:
                self.assert_relocation_current(source, target, manifest)
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
                if self.canonical_rag_managed_externally and native_relocation_proof is not None:
                    relocation = manifest.get('rag_relocation')
                    inverse = {'from': relocation['to'], 'to': relocation['from']} if relocation is not None else None
                    native_relocation_proof.assert_current(target, source, {'rag_relocation': inverse})
                elif self.canonical_rag_managed_externally:
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
                    elif getattr(self.rag_manager, "backend", None) == "memery":
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
                if native_relocation_proof is not None:
                    relocation = manifest.get('rag_relocation')
                    reverse['rag_relocation'] = ({'from': relocation['to'], 'to': relocation['from']}
                        if relocation is not None else None)
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

            restored_receipt = self.reconcile_owner_rename(target, source, reverse, native_relocation_proof=native_relocation_proof)
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
                if getattr(rag, "backend", "") == "memery":
                    if rag.add_document(text, metadata):
                        from src.memery_rag import _chunks

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

    async def _guarded_personal_source_held(request, path, observe, *, deadline, owner, account, credential, state, auth, barrier, capture, loop, extra_current=None):
        """Run under the existing owner lane, using its already captured authority."""
        from src.openclank.account_request_barrier import _current_admission
        from src.openclank.memory_job_admission import _completed_turn_thread
        from src.openclank.personal_document_source import _retained_personal_document
        async with barrier.admitted(owner):
            admission = _current_admission.get()
            def current(snapshot, source=True):
                try:
                    barrier.assert_live_admission(owner, admission)
                    return (getattr(state, 'account_request_barrier', None) is barrier
                        and getattr(state, 'auth_manager', None) is auth
                        and getattr(state, 'capture_original_credential', None) is capture
                        and loop.time() < deadline and credential() is True
                        and require_privilege(request, 'can_use_documents') == owner
                        and auth.account_id(owner) == account
                        and auth.username_for_account_id(account) == owner
                        and (extra_current is None or extra_current() is True)
                        and (not source or snapshot.assert_current() is True))
                except Exception:
                    return False
            async def inspect(snapshot):
                if not current(snapshot):
                    raise ValueError('personal document authority unavailable')
                async with asyncio.timeout_at(deadline):
                    authority = _PersonalDocumentAuthority(owner, account, snapshot,
                        lambda source: current(snapshot, source), _PERSONAL_DOCUMENT_AUTHORITY_SEAL)
                    result = await observe(authority)
                if not current(snapshot, False):
                    raise ValueError('personal document authority changed')
                return result
            def worker():
                def current_on_loop():
                    async def check():
                        return current(None, False)
                    future = asyncio.run_coroutine_threadsafe(check(), loop)
                    try:
                        return future.result(timeout=max(0.0, deadline - loop.time())) is True
                    except Exception:
                        future.cancel()
                        return False
                selected = path(current_on_loop) if callable(path) else path
                with _retained_personal_document(owner, selected, UPLOADS_DIR) as snapshot:
                    return asyncio.run_coroutine_threadsafe(inspect(snapshot), loop).result()
            result, cancelled = await _completed_turn_thread(worker)
            if cancelled:
                raise asyncio.CancelledError
            return result


    async def _guarded_personal_source(request, path, observe, *, deadline, extra_current=None):
        """Hold the real personal source owners while a private observer runs.

        This source witness does not enable a document writer. The observer must
        provide its own operation authority and exact original outcome handling.
        """
        from src.openclank.account_request_barrier import AccountRequestBarrier
        if (not callable(observe) or type(deadline) is not float or not math.isfinite(deadline)
                or getattr(request.state, 'api_token', False)
                or getattr(request.state, 'internal_tool_authenticated', False)):
            raise ValueError('personal document authority unavailable')
        owner = require_privilege(request, 'can_use_documents')
        state = request.app.state
        auth = getattr(state, 'auth_manager', None)
        barrier = getattr(state, 'account_request_barrier', None)
        capture = getattr(state, 'capture_original_credential', None)
        if (auth is None or type(barrier) is not AccountRequestBarrier or not callable(capture)):
            raise ValueError('personal document authority unavailable')
        account = auth.account_id(owner)
        if not account or auth.username_for_account_id(account) != owner:
            raise ValueError('personal document authority unavailable')
        credential = capture(request, owner, account)
        loop = asyncio.get_running_loop()
        if loop.time() >= deadline:
            raise ValueError('personal document authority unavailable')
        async with asyncio.timeout_at(deadline):
            async with _owner_lifecycle_lock(owner):
                return await _guarded_personal_source_held(request, path, observe, deadline=deadline,
                    owner=owner, account=account, credential=credential, state=state, auth=auth,
                    barrier=barrier, capture=capture, loop=loop, extra_current=extra_current)

    async def _guarded_personal_original(request, observe, *, deadline):
        """Current named-account admission for a factual original read only."""
        from src.openclank.account_request_barrier import AccountRequestBarrier, _current_admission
        if (not callable(observe) or type(deadline) is not float or not math.isfinite(deadline)
                or getattr(request.state, 'api_token', False)
                or getattr(request.state, 'internal_tool_authenticated', False)):
            raise ValueError('personal document authority unavailable')
        owner = require_privilege(request, 'can_use_documents')
        state=request.app.state; auth=getattr(state,'auth_manager',None)
        barrier=getattr(state,'account_request_barrier',None); capture=getattr(state,'capture_original_credential',None)
        if auth is None or type(barrier) is not AccountRequestBarrier or not callable(capture):
            raise ValueError('personal document authority unavailable')
        account=auth.account_id(owner)
        if not account or auth.username_for_account_id(account)!=owner:
            raise ValueError('personal document authority unavailable')
        credential=capture(request,owner,account); loop=asyncio.get_running_loop()
        async with asyncio.timeout_at(deadline):
            async with barrier.admitted(owner):
                admission=_current_admission.get()
                def current(source):
                    if source: return False
                    try:
                        barrier.assert_live_admission(owner,admission)
                        return (getattr(state,'account_request_barrier',None) is barrier
                            and getattr(state,'auth_manager',None) is auth
                            and getattr(state,'capture_original_credential',None) is capture
                            and loop.time()<deadline and credential() is True
                            and require_privilege(request,'can_use_documents')==owner
                            and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner)
                    except Exception: return False
                authority=_PersonalDocumentAuthority(owner,account,None,current,_PERSONAL_DOCUMENT_AUTHORITY_SEAL)
                if authority.current_account() is not True: raise ValueError('personal document authority unavailable')
                async with asyncio.timeout_at(deadline):
                    result=await observe(authority)
                if authority.current_account() is not True: raise ValueError('personal document authority changed')
                return result

    async def _guarded_personal_bulk(request, observe, *, deadline):
        """Hold one original named-human owner lane for a retained-snapshot job.

        This is an account admission only. The internal observer must verify
        the exact current Native document receipts, chunks and watermark at
        each producer batch and finalization; no model or route is enabled.
        """
        from src.openclank.account_request_barrier import AccountRequestBarrier
        if (not callable(observe) or type(deadline) is not float or not math.isfinite(deadline)
                or getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)):
            raise ValueError('personal document authority unavailable')
        owner=require_privilege(request,'can_use_documents');state=request.app.state
        auth=getattr(state,'auth_manager',None);barrier=getattr(state,'account_request_barrier',None)
        capture=getattr(state,'capture_original_credential',None);loop=asyncio.get_running_loop()
        if auth is None or type(barrier) is not AccountRequestBarrier or not callable(capture):
            raise ValueError('personal document authority unavailable')
        account=auth.account_id(owner)
        if not account or auth.username_for_account_id(account)!=owner or loop.time()>=deadline:
            raise ValueError('personal document authority unavailable')
        credential=capture(request,owner,account)
        def outer_current():
            try:
                return (getattr(state,'account_request_barrier',None) is barrier
                    and getattr(state,'auth_manager',None) is auth and getattr(state,'capture_original_credential',None) is capture
                    and loop.time()<deadline and credential() is True
                    and require_privilege(request,'can_use_documents')==owner
                    and auth.account_id(owner)==account and auth.username_for_account_id(account)==owner)
            except Exception:return False
        async def inspect(authority):
            if not outer_current() or authority.current_account() is not True:
                raise ValueError('personal document authority changed')
            result=await observe(authority)
            if not outer_current() or authority.current_account() is not True:
                raise ValueError('personal document authority changed')
            return result
        async with asyncio.timeout_at(deadline):
            async with _owner_lifecycle_lock(owner):
                if not outer_current():raise ValueError('personal document authority changed')
                return await _guarded_personal_original(request,inspect,deadline=deadline)

    async def _guarded_document_vector_intent(request, *, operation_id, action, deadline):
        """Use only the current human document lane and its original credential."""
        from src.embeddings import ManagedEmbeddingClient
        from src.memery_provider import MemeryProvider
        if action not in ('prepare','execute','status','prepare_v2','execute_v2','resume_v2','status_v2','cancel_v2') or type(operation_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{16,128}',operation_id) is None:
            raise ValueError('document vector original unavailable')
        async def operate(authority):
            rag=get_rag_manager();factory=getattr(rag,'_embedding_client',None);state=request.app.state
            provider=getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)
            if type(provider) is not MemeryProvider or not callable(getattr(factory,'for_owner',None)):
                raise ValueError('document vector producer unavailable')
            selected=factory.for_owner(authority.owner)
            if type(selected) is not ManagedEmbeddingClient or selected.owner!=authority.owner:
                raise ValueError('document vector producer unavailable')
            client=selected.for_root_operation(operation_id)
            def current(source):
                try:
                    return (not source and authority.current_account() is True and get_rag_manager() is rag
                        and getattr(rag,'_embedding_client',None) is factory and factory.for_owner(authority.owner) is selected
                        and (getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)) is provider)
                except Exception:return False
            scoped=_PersonalDocumentAuthority(authority.owner,authority.account,None,current,_PERSONAL_DOCUMENT_AUTHORITY_SEAL)
            if scoped.current_account() is not True:raise ValueError('document vector producer changed')
            if action=='prepare':return await provider._host_document_vector_intent_prepare(scoped,client,operation_id=operation_id,deadline=deadline)
            if action=='execute':return await provider._host_document_vector_intent_execute(scoped,client,operation_id=operation_id,deadline=deadline)
            if action=='cancel_v2':return await provider._host_document_vector_v2_cancel(scoped,client,operation_id=operation_id,deadline=deadline)
            if action=='prepare_v2':return await provider._host_document_vector_intent_prepare(scoped,client,operation_id=operation_id,deadline=deadline,version=2)
            if action in ('execute_v2','resume_v2'):return await provider._host_document_vector_v2_step(scoped,client,operation_id=operation_id,deadline=deadline,action='execute' if action=='execute_v2' else 'resume')
            return await provider._host_document_vector_intent_status(scoped,client,operation_id=operation_id,deadline=deadline,version=2 if action=='status_v2' else 1)
        return await _guarded_personal_bulk(request,operate,deadline=deadline)

    async def _guarded_document_vector_prepare(request, *, deadline):
        # The operation ID is returned only after the original is durable, and
        # this preparation never starts a model or sends an embedding frame.
        operation_id='vecbuild_'+uuid.uuid4().hex
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='prepare',deadline=deadline)

    async def _guarded_document_vector_build(request, *, operation_id, deadline):
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='execute',deadline=deadline)

    async def _guarded_document_vector_status(request, *, operation_id, deadline):
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='status',deadline=deadline)

    async def _guarded_document_vector_v2_prepare(request, *, deadline):
        operation_id='vecbuild_'+uuid.uuid4().hex
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='prepare_v2',deadline=deadline)

    async def _guarded_document_vector_v2_execute(request, *, operation_id, deadline):
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='execute_v2',deadline=deadline)

    async def _guarded_document_vector_v2_resume(request, *, operation_id, deadline):
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='resume_v2',deadline=deadline)

    async def _guarded_document_vector_v2_status(request, *, operation_id, deadline):
        return await _guarded_document_vector_intent(request,operation_id=operation_id,action='status_v2',deadline=deadline)

    @router.get('/vector-build/capability')
    async def document_vector_build_capability(request: Request):
        from src.memory_provider import MemoryRequestRejectedError
        from src.openclank.operation_router import ManagedOperationError
        from src.embeddings import ManagedEmbeddingError
        from src.embeddings import ManagedEmbeddingClient, ManagedEmbeddingClientFactory
        from src.memery_provider import MemeryProvider
        operation_id='vec_cap_'+uuid.uuid4().hex
        async def inspect(authority):
            rag=get_rag_manager();state=request.app.state;factory=getattr(rag,'_embedding_client',None)
            provider=getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)
            if type(factory) is not ManagedEmbeddingClientFactory or type(provider) is not MemeryProvider:
                raise ValueError('document vector capability unavailable')
            client=ManagedEmbeddingClient(owner=authority.owner,root_operation_id=operation_id)
            def current(source):
                try:return (not source and authority.current_account() is True and get_rag_manager() is rag
                    and getattr(rag,'_embedding_client',None) is factory
                    and (getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)) is provider)
                except Exception:return False
            scoped=_PersonalDocumentAuthority(authority.owner,authority.account,None,current,_PERSONAL_DOCUMENT_AUTHORITY_SEAL)
            return await provider._host_document_vector_v2_capability(scoped,client,operation_id=operation_id,deadline=float(asyncio.get_running_loop().time()+10.0))
        try:
            return await _guarded_personal_bulk(request,inspect,deadline=float(asyncio.get_running_loop().time()+15.0))
        except (ValueError, TimeoutError, MemoryRequestRejectedError, ManagedOperationError, ManagedEmbeddingError):
            raise HTTPException(409,'Document embedding build unavailable')

    @router.post('/vector-build/prepare')
    async def document_vector_build_prepare(request: Request):
        from src.memory_provider import MemoryRequestRejectedError
        from src.openclank.operation_router import ManagedOperationError
        from src.embeddings import ManagedEmbeddingError
        try:
            return await _guarded_document_vector_v2_prepare(request,deadline=float(asyncio.get_running_loop().time()+30.0))
        except (ValueError, TimeoutError, MemoryRequestRejectedError, ManagedOperationError, ManagedEmbeddingError):
            raise HTTPException(409,'Document embedding preparation unavailable')

    async def _document_vector_build_action(request: Request, body: dict, action: str):
        from src.memory_provider import MemoryRequestRejectedError
        from src.openclank.operation_router import ManagedOperationError
        from src.embeddings import ManagedEmbeddingError
        if type(body) is not dict or set(body)!={'operation_id'} or type(body.get('operation_id')) is not str:
            raise HTTPException(400,'Document build ID required')
        try:
            return await _guarded_document_vector_intent(request,operation_id=body['operation_id'],action=action,deadline=float(asyncio.get_running_loop().time()+(120.0 if action in ('execute_v2','resume_v2') else 30.0)))
        except (ValueError, TimeoutError, MemoryRequestRejectedError, ManagedOperationError, ManagedEmbeddingError):
            raise HTTPException(409,'Document build unavailable or outcome unconfirmed')

    @router.post('/vector-build/execute')
    async def document_vector_build_execute(request: Request, body: dict):
        return await _document_vector_build_action(request,body,'execute_v2')

    @router.post('/vector-build/status')
    async def document_vector_build_status(request: Request, body: dict):
        return await _document_vector_build_action(request,body,'status_v2')

    @router.post('/vector-build/resume')
    async def document_vector_build_resume(request: Request, body: dict):
        return await _document_vector_build_action(request,body,'resume_v2')

    @router.post('/vector-build/cancel')
    async def document_vector_build_cancel(request: Request, body: dict):
        return await _document_vector_build_action(request,body,'cancel_v2')

    async def _guarded_document_vector_profile_build(request, *, deadline):
        """Explicit CPU-only profile creation from the current canonical generation."""
        from src.memery_provider import MemeryProvider
        async def build(authority):
            state=request.app.state;rag=get_rag_manager();factory=getattr(rag,'_embedding_client',None)
            provider=getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)
            if type(provider) is not MemeryProvider or rag is None:
                raise ValueError('document vector profile unavailable')
            def current(source):
                try:
                    return (not source and authority.current_account() is True and get_rag_manager() is rag
                        and getattr(rag,'_embedding_client',None) is factory
                        and (getattr(state,'canonical_memory_provider',None) or getattr(state,'memory_provider',None)) is provider)
                except Exception:return False
            scoped=_PersonalDocumentAuthority(authority.owner,authority.account,None,current,_PERSONAL_DOCUMENT_AUTHORITY_SEAL)
            if scoped.current_account() is not True:raise ValueError('document vector profile authority changed')
            return await provider._host_document_vector_profile_build(scoped,deadline=deadline)
        return await _guarded_personal_bulk(request,build,deadline=deadline)

    @router.post('/vector-profile/build')
    async def build_document_vector_profile(request: Request):
        from src.memory_provider import MemoryRequestRejectedError
        try:
            return await _guarded_document_vector_profile_build(request,deadline=float(asyncio.get_running_loop().time()+120.0))
        except (ValueError, TimeoutError, MemoryRequestRejectedError):
            raise HTTPException(409,'Document vector profile unavailable or its outcome could not be confirmed')

    # Private source and factual original only; no user-facing route is enabled.
    router._guarded_personal_source = _guarded_personal_source
    router._guarded_personal_original = _guarded_personal_original
    router._guarded_personal_bulk = _guarded_personal_bulk
    router._guarded_document_vector_prepare = _guarded_document_vector_prepare
    router._guarded_document_vector_build = _guarded_document_vector_build
    router._guarded_document_vector_status = _guarded_document_vector_status
    router._guarded_document_vector_v2_prepare = _guarded_document_vector_v2_prepare
    router._guarded_document_vector_v2_execute = _guarded_document_vector_v2_execute
    router._guarded_document_vector_v2_resume = _guarded_document_vector_v2_resume
    router._guarded_document_vector_v2_status = _guarded_document_vector_v2_status
    router._guarded_document_vector_profile_build = _guarded_document_vector_profile_build

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
    
    @router.post("/document-structure/resume/{name}")
    async def resume_personal_document_structure(name: str, request: Request):
        from src.memery_provider import MemeryProvider
        from src.openclank.document_ingestion import personal
        if name != os.path.basename(name) or name in {"", ".", ".."} or len(name.encode()) > 255:
            raise HTTPException(422, "Invalid saved document name")
        owner = require_privilege(request, 'can_use_documents')
        state = request.app.state
        provider = getattr(state,"canonical_memory_provider",None) or getattr(state,"memory_provider",None)
        if type(provider) is not MemeryProvider:
            raise HTTPException(503,"Document structure is unavailable")
        deadline = asyncio.get_running_loop().time() + 10
        async def continue_source(authority):
            head = await provider._host_document_head(authority,deadline=deadline)
            if (head["state"] != "present" or head["forget_state"] != "active"
                    or head["content_sha256"] != hashlib.sha256(authority.snapshot.text.encode()).hexdigest()):
                raise ValueError("Indexed source changed")
            return await personal(provider,authority,source_id=head["source_id"],
                                  source_revision=head["source_revision"],deadline=deadline)
        try:
            path = os.path.join(_personal_upload_dir_for_owner(owner,create=False),name)
            return await _guarded_personal_source(request,path,continue_source,deadline=deadline)
        except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
            raise HTTPException(409,'Saved source or document structure is unavailable') from exc

    @router.post("/upload")
    async def upload_files_to_rag(request: Request, files: List[UploadFile] = File(...)):
        """Upload files directly into RAG. Supports text and PDF."""
        user = require_privilege(request, "can_use_documents")
        from src.openclank.chat_source_bridge import canonical_source_provider
        from src.openclank.account_request_barrier import AccountRequestBarrier
        state = request.app.state
        provider = canonical_source_provider(state)
        guarded = provider is not None and getattr(provider, '_guarded_v17', False) is True
        if not guarded and os.environ.get('OPENCLANK_MEMORY_STORE_PROFILE', 'legacy-v16') in ('guarded-v17', 'guarded-v18'):
            raise HTTPException(503, "Guarded document indexing is unavailable")
        rag = None if guarded else _rag()
        if not guarded and not rag:
            raise HTTPException(503, "RAG system is not available — is the embedding service running?")
        if guarded:
            auth=getattr(state,'auth_manager',None); barrier=getattr(state,'account_request_barrier',None)
            capture=getattr(state,'capture_original_credential',None)
            if (getattr(request.state,'api_token',False) or getattr(request.state,'internal_tool_authenticated',False)
                    or auth is None or type(barrier) is not AccountRequestBarrier or not callable(capture)):
                raise HTTPException(503, "Guarded document indexing is unavailable")
            account=auth.account_id(user)
            if not account or auth.username_for_account_id(account)!=user:
                raise HTTPException(503, "Guarded document indexing is unavailable")
            credential=capture(request,user,account)
            if await provider._host_document_import_ready() is not True or canonical_source_provider(state) is not provider:
                raise HTTPException(503, "Guarded document indexing is unavailable")

        pending: list[tuple[UploadFile, bytes]] = []
        oversized = 0
        for upload in files:
            content_bytes = await upload.read(PERSONAL_UPLOAD_MAX_BYTES + 1)
            if len(content_bytes) > PERSONAL_UPLOAD_MAX_BYTES:
                logger.warning("Rejected oversized personal upload: %r", upload.filename)
                oversized += 1
            else:
                pending.append((upload, content_bytes))

        if guarded:
            loop=asyncio.get_running_loop(); uploaded=[]; outcomes=[]; indexed=0
            async with _owner_lifecycle_lock(user):
                if await provider._host_document_import_ready() is not True or canonical_source_provider(state) is not provider:
                    raise HTTPException(503, "Guarded document indexing is unavailable")
                for upload, content_bytes in pending:
                    saved={}; write_status={}; operation_id=uuid.uuid4().hex
                    deadline=float(loop.time()+30.0)
                    def save(check):
                        lock_target=os.path.join(os.path.abspath(UPLOADS_DIR), '.owner-lifecycle.json')
                        with locked(lock_target):
                            if check() is not True or canonical_source_provider(state) is not provider:
                                raise ValueError('personal document authority changed')
                            directory=_personal_upload_dir_for_owner(user)
                            file_path, _stored_name, safe_name=_unique_personal_upload_path(directory,upload.filename)
                            flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os,'O_CLOEXEC',0)
                            root_fd=os.open(os.path.abspath(UPLOADS_DIR),flags); dir_fd=None
                            try:
                                dir_fd=os.open(os.path.basename(directory),flags,dir_fd=root_fd)
                                fd=os.open(os.path.basename(file_path),os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,0o600,dir_fd=dir_fd)
                                with os.fdopen(fd,'wb') as stream:
                                    stream.write(content_bytes); stream.flush(); os.fsync(stream.fileno())
                                    st=os.fstat(stream.fileno()); directory_stat=os.fstat(dir_fd); root_stat=os.fstat(root_fd)
                                    saved.update(path=file_path,name=safe_name,
                                        fingerprint=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns,st.st_ctime_ns),
                                        directory=(directory_stat.st_dev,directory_stat.st_ino), root=(root_stat.st_dev,root_stat.st_ino))
                            finally:
                                if dir_fd is not None: os.close(dir_fd)
                                os.close(root_fd)
                            return file_path
                    async def apply(authority):
                        snapshot=authority.snapshot
                        if (canonical_source_provider(state) is not provider or snapshot._fingerprint != saved.get('fingerprint')
                                or snapshot._directory_identity != saved.get('directory') or snapshot._root_identity != saved.get('root')):
                            raise ValueError('personal document source changed')
                        head=await provider._host_document_head(authority,deadline=deadline)
                        expected=({'kind':'absent'} if head['state']=='absent' else
                            {'kind':'revision','source_id':head['source_id'],'revision':head['source_revision'],'content_sha256':head['content_sha256'],'forget_state':head['forget_state']})
                        result=await provider._host_document_upsert(authority,operation_id=operation_id,
                            expected_head=expected,deadline=deadline,write_status=write_status)
                        if result.get('state')!='committed': raise ValueError('personal document outcome unknown')
                        from src.openclank.document_ingestion import personal
                        structure = await personal(provider, authority, source_id=result["source_id"],
                            source_revision=result["source_revision"], deadline=min(deadline - 0.25, loop.time() + 4))
                        return result['chunk_count'], structure
                    try:
                        async with asyncio.timeout_at(deadline):
                            count,structure=await _guarded_personal_source_held(request,save,apply,deadline=deadline,owner=user,
                                account=account,credential=credential,state=state,auth=auth,barrier=barrier,capture=capture,loop=loop,
                                extra_current=lambda: canonical_source_provider(state) is provider)
                        indexed+=count; uploaded.append(saved['name'])
                        outcomes.append({'name':saved['name'],'state':'indexed','documentStructure':structure,
                                         'storedName':os.path.basename(saved['path'])})
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        ticket=write_status.get('ticket'); item={'name':saved.get('name'),'state':'saved_unindexed' if saved else 'not_saved'}
                        if type(ticket) is dict and not (ticket.get('issued') is False and ticket.get('owned_io_drained') is True):
                            item['state']='outcome_unknown'
                        payload=ticket.get('owned_original_payload') if type(ticket) is dict else None
                        original=payload.get('request') if type(payload) is dict else None
                        if type(original) is dict:
                            encoded=json.dumps(original,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
                            item['original']={'version':1,'operation_id':original['operation_id'],
                                'request_sha256':hashlib.sha256(b'openclank.document-upsert.request.v1\n'+encoded).hexdigest(),
                                'account_id':account,'current_owner':user,'captured_owner':original['captured_owner']}
                        outcomes.append(item)
            failures=sum(item['state']!='indexed' for item in outcomes)+oversized
            return {'success':failures==0,'uploaded':uploaded,'indexed_count':indexed,'failed_count':failures,
                    'outcomes':outcomes}

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
    async def delete_file_from_rag(request: Request, filepath: str = Query(...), owner: str = Depends(require_user), _admin: None = Depends(require_admin)):
        """Delete a specific file from RAG index and optionally from disk."""
        from src.openclank.chat_source_bridge import canonical_source_provider
        provider=canonical_source_provider(request.app.state)
        guarded=(provider is not None and getattr(provider,'_guarded_v17',False) is True)
        if guarded or os.environ.get('OPENCLANK_MEMORY_STORE_PROFILE', 'legacy-v16') in ('guarded-v17', 'guarded-v18'):
            if not guarded:
                raise HTTPException(503, 'Guarded document removal is unavailable')
            loop=asyncio.get_running_loop(); deadline=float(loop.time()+30.0); write_status={}; unlinked=[False]
            operation_id=uuid.uuid4().hex
            async def remove(authority):
                if (authority.owner != owner or canonical_source_provider(request.app.state) is not provider
                        or await provider._host_document_import_ready() is not True or authority.current_source() is not True):
                    raise ValueError('personal document authority changed')
                head=await provider._host_document_head(authority,deadline=deadline)
                removed=0
                active = head['state']=='present' and head['forget_state']=='active'
                if active:
                    result=await provider._host_document_remove(authority,operation_id=operation_id,source_id=head['source_id'],
                        expected_head={'revision':head['source_revision'],'content_sha256':head['content_sha256']},
                        deadline=deadline,write_status=write_status)
                    if result.get('state')!='removed': raise ValueError('personal document outcome unknown')
                    removed=result['chunks_removed']
                if authority.current_source() is not True or canonical_source_provider(request.app.state) is not provider:
                    raise ValueError('personal document authority changed')
                snapshot=authority.snapshot
                snapshot.assert_current()
                os.unlink(snapshot._path.name,dir_fd=snapshot._directory_fd)
                unlinked[0]=True
                return {'success':True,'removed_chunks':removed,'deleted_from_disk':True,
                    'index_state':'removed' if active else 'absent'}
            try:
                return await _guarded_personal_source(request,filepath,remove,deadline=deadline,
                    extra_current=lambda: canonical_source_provider(request.app.state) is provider)
            except asyncio.CancelledError:
                raise
            except Exception:
                ticket=write_status.get('ticket'); item={'success':False,'deleted_from_disk':unlinked[0],'state':'unavailable'}
                if type(ticket) is dict and not (ticket.get('issued') is False and ticket.get('owned_io_drained') is True):
                    item['state']='outcome_unknown'
                payload=ticket.get('owned_original_payload') if type(ticket) is dict else None
                original=payload.get('request') if type(payload) is dict else None
                if type(original) is dict:
                    encoded=json.dumps(original,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
                    item['original']={'version':1,'operation_id':original['operation_id'],
                        'request_sha256':hashlib.sha256(b'openclank.document-remove.request.v1\n'+encoded).hexdigest(),
                        'account_id':original['account_id'],'current_owner':owner,'captured_owner':original['captured_owner']}
                return item
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
                        # canonical Memery implementation accepts owner;
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

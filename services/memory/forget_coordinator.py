"""Keep memory-derived promotions and skills inside the forget boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

from core.atomic_io import _fsync_directory, atomic_write_json
from src.memory_provider import MemoryRequestRejectedError
from src.memory_scope import chat_workspace, memory_owner

from .skill_lifecycle import load_state, locked


_TOKEN_PREFIX = "oc-memory-forget-v1."
_RECOVERY_LIMIT = 100
_PLACEHOLDER = ".memory-forgotten.json"
_SKILL_LOCK = ".lifecycle.lock"

logger = logging.getLogger(__name__)


def require_memory_lifecycle_convergence(counts: dict[str, object]) -> None:
    """Keep unresolved provider/local splits outside the readiness boundary."""
    if int(counts.get("errors") or 0):
        raise RuntimeError("memory lifecycle reconciliation did not converge")


def _sync_directories(*directories: Path) -> None:
    """Persist directory-entry changes before crossing the provider boundary."""
    seen: set[str] = set()
    for directory in directories:
        value = os.fspath(directory)
        if value in seen or not directory.is_dir():
            continue
        seen.add(value)
        _fsync_directory(value)


def pack_forget_token(kind: str, **parts: str) -> str:
    payload = {"kind": kind, **parts}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return _TOKEN_PREFIX + encoded


def unpack_forget_token(token: str, kind: str) -> dict[str, str]:
    if not isinstance(token, str) or not token.startswith(_TOKEN_PREFIX):
        raise ValueError("invalid composite memory forget token")
    encoded = token[len(_TOKEN_PREFIX):]
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid composite memory forget token") from exc
    if not isinstance(payload, dict) or payload.get("kind") != kind:
        raise ValueError("invalid composite memory forget token")
    values = {
        str(key): str(value)
        for key, value in payload.items()
        if key != "kind" and isinstance(value, str) and value
    }
    if len(values) != len(payload) - 1:
        raise ValueError("invalid composite memory forget token")
    return values


class MemorySkillForgetCoordinator:
    """Reversibly quarantine owner-scoped artifacts derived from memories."""

    def __init__(self, skills_manager):
        self.skills = skills_manager
        data_root = Path(skills_manager.data_dir).resolve()
        self.recovery_root = data_root / ".memory-forget-skills"
        if self.recovery_root.is_symlink():
            raise ValueError("memory-derived skill recovery root cannot be a symlink")
        self.lock_target = os.path.join(
            skills_manager.skills_root,
            "PROMOTIONS",
        )
        with locked(self.lock_target):
            self._recover_staging()

    @staticmethod
    def _memory_ids(values: Iterable[object]) -> list[str]:
        return sorted({str(value) for value in values if str(value or "")})

    @staticmethod
    def _workspace_id(value: object = None) -> str:
        """Use one durable spelling for the conversational workspace."""
        return str(value or "").strip() or chat_workspace()

    @staticmethod
    def _citation_ids(row: dict) -> set[str]:
        return {
            str(item.get("memory_id"))
            for item in row.get("citations", ())
            if isinstance(item, dict) and item.get("memory_id")
        }

    def _snapshot(self, owner: str, memory_ids: Iterable[object]) -> dict:
        selected = set(self._memory_ids(memory_ids))
        promotions = []
        promotion_ids: set[str] = set()
        linked_skill_ids: set[str] = set()
        for row in self.skills._load_promotions():
            if row.get("owner") != owner or not self._citation_ids(row) & selected:
                continue
            promotion_id = row.get("id")
            if not isinstance(promotion_id, str) or not promotion_id:
                raise ValueError("memory promotion has no stable identity")
            if promotion_id in promotion_ids:
                raise ValueError("memory promotion identity is duplicated")
            promotion_ids.add(promotion_id)
            if row.get("skill_id"):
                linked_skill_ids.add(str(row["skill_id"]))
            promotions.append(row)
        promotions.sort(key=lambda row: row["id"])

        skill_rows = []
        for path in self.skills._iter_skill_files() or ():
            skill = self.skills._read_skill(path)
            linked = (
                bool(set(skill.source_memory_ids) & selected)
                or skill.skill_id in linked_skill_ids
                or (
                    isinstance(skill.source_uri, str)
                    and skill.source_uri.startswith("memory-promotion:")
                    and skill.source_uri.removeprefix("memory-promotion:")
                    in promotion_ids
                )
            ) if skill is not None else False
            if (
                skill is None
                or str(skill.owner or "") != owner
                or not linked
            ):
                continue
            if not skill.skill_id:
                raise ValueError("memory-derived skill has no stable identity")
            relative = Path(path).parent.relative_to(
                Path(self.skills.skills_root)
            ).as_posix()
            skill_rows.append({
                "skill_id": skill.skill_id,
                "name": skill.name,
                "relative_dir": relative,
                "revision": skill.revision,
                "content_hash": skill.content_hash,
                "source_memory_ids": sorted(skill.source_memory_ids),
                "published": load_state(path).get("published"),
                "_path": path,
            })
        skill_rows.sort(key=lambda row: (row["relative_dir"], row["skill_id"]))
        token_payload = {
            "owner": owner,
            "memory_ids": sorted(selected),
            "promotions": promotions,
            "skills": [
                {key: value for key, value in row.items() if key != "_path"}
                for row in skill_rows
            ],
        }
        token = hashlib.sha256(
            json.dumps(
                token_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
        return {
            **token_payload,
            "token": token,
            "_skill_rows": skill_rows,
        }

    @staticmethod
    def _public(snapshot: dict) -> dict:
        return {
            "token": snapshot["token"],
            "promotion_ids": [
                str(row.get("id"))
                for row in snapshot["promotions"]
                if row.get("id")
            ],
            "skill_ids": [
                str(row.get("skill_id"))
                for row in snapshot["skills"]
                if row.get("skill_id")
            ],
        }

    def preview(self, owner: str, memory_ids: Iterable[object]) -> dict:
        with locked(self.lock_target):
            self._recover_staging()
            return self._public(self._snapshot(owner, memory_ids))

    @staticmethod
    def _validate_recovery_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 32
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise ValueError("invalid memory-derived skill tombstone")
        return value

    @staticmethod
    def _relative_dir(row: dict) -> PurePosixPath:
        value = str(row.get("relative_dir") or "")
        relative = PurePosixPath(value)
        if (
            not value
            or "\x00" in value
            or "\\" in value
            or ":" in value
            or relative.is_absolute()
            or len(relative.parts) != 2
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ValueError("invalid recovered skill path")
        return relative

    def _active_dir(self, row: dict) -> Path:
        relative = self._relative_dir(row)
        root = Path(self.skills.skills_root).resolve()
        destination = root.joinpath(*relative.parts)
        if root not in destination.resolve(strict=False).parents:
            raise ValueError("recovered skill path escapes the skills root")
        return destination

    @staticmethod
    def _read_marker(path: Path) -> dict:
        try:
            value = json.loads((path / _PLACEHOLDER).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("memory-derived skill placeholder is missing") from exc
        return value if isinstance(value, dict) else {}

    def _restore_local_path(
        self,
        recovery: Path,
        manifest: dict,
        *,
        remove_markers: bool = True,
    ) -> list[tuple[Path, Path]]:
        moved: list[tuple[Path, Path]] = []
        for row in manifest.get("skills") or ():
            relative = self._relative_dir(row)
            source = recovery / "skills" / Path(*relative.parts)
            destination = self._active_dir(row)
            if source.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                for entry in source.iterdir():
                    target = destination / entry.name
                    if target.exists() or target.is_symlink():
                        raise ValueError("recovered skill path now conflicts")
                    os.replace(entry, target)
                    moved.append((entry, target))
                _sync_directories(source, destination, destination.parent)
            marker_path = destination / _PLACEHOLDER
            if remove_markers and marker_path.exists():
                marker = self._read_marker(destination)
                if marker.get("id") != manifest.get("id"):
                    raise ValueError("memory-derived skill placeholder is out of scope")
                marker_path.unlink()
                _sync_directories(destination)
        return moved

    def _merge_promotions(self, manifest: dict) -> None:
        active = self.skills._load_promotions()
        changed = False
        for restored in manifest.get("promotions") or ():
            owner = restored.get("owner")
            promotion_id = restored.get("id")
            matches = [
                row
                for row in active
                if row.get("owner") == owner and row.get("id") == promotion_id
            ]
            if matches:
                if len(matches) != 1 or matches[0] != restored:
                    raise ValueError("promotion identity now conflicts with recovery")
                continue
            active.append(restored)
            changed = True
        if changed:
            self.skills._save_promotions(active)

    def _rollback_recovery(self, path: Path, manifest: dict) -> None:
        self._restore_local_path(path, manifest)
        self._merge_promotions(manifest)
        shutil.rmtree(path)
        _sync_directories(self.recovery_root)

    def _recover_staging(self) -> None:
        if self.recovery_root.is_symlink():
            raise ValueError("memory-derived skill recovery root cannot be a symlink")
        if not self.recovery_root.is_dir():
            return
        for path in sorted(self.recovery_root.glob(".staging-*")):
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                manifest = json.loads(
                    (path / "manifest.json").read_text(encoding="utf-8")
                )
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("version") != 1
                    or manifest.get("state") not in {"preparing", "prepared"}
                ):
                    raise ValueError("invalid staging manifest")
                self._validate_recovery_id(str(manifest.get("id") or ""))
                if not isinstance(manifest.get("promotions"), list) or not isinstance(
                    manifest.get("skills"),
                    list,
                ):
                    raise ValueError("invalid staging manifest")
                for row in manifest["skills"]:
                    if not isinstance(row, dict):
                        raise ValueError("invalid staged skill identity")
                    self._relative_dir(row)
                self._rollback_recovery(path, manifest)
            except Exception as exc:
                logger.exception(
                    "Could not roll back staged memory-derived skill recovery %s",
                    path.name,
                )
                raise ValueError(
                    "memory-derived skill staging recovery is incomplete"
                ) from exc

    def _discard_recovery_payload(self, path: Path, manifest: dict) -> None:
        for row in manifest.get("skills") or ():
            destination = self._active_dir(row)
            if not destination.is_dir() or destination.is_symlink():
                continue
            try:
                marker = self._read_marker(destination)
            except ValueError:
                continue
            if marker.get("id") != manifest.get("id"):
                continue
            unexpected = {
                entry.name
                for entry in destination.iterdir()
                if entry.name not in {_PLACEHOLDER, _SKILL_LOCK}
            }
            if unexpected:
                raise ValueError("forgotten skill placeholder was reused")
            shutil.rmtree(destination)
            _sync_directories(destination.parent)
        shutil.rmtree(path / "skills", ignore_errors=True)
        _sync_directories(path)

    def _purge_recovery(self, path: Path, manifest: dict) -> None:
        self._discard_recovery_payload(path, manifest)
        shutil.rmtree(path)
        _sync_directories(self.recovery_root)

    def _seal_irrecoverable(
        self,
        path: Path,
        manifest: dict,
        *,
        provider_tombstone_id: str,
    ) -> None:
        """Keep a tiny idempotency ledger after discarding recovery payload."""
        manifest.update({
            "state": "committed_irrecoverable",
            "provider_tombstone_id": str(provider_tombstone_id),
            "recover_until": None,
            "expires_at": None,
            "sealed_at": time.time(),
        })
        atomic_write_json(
            str(path / "manifest.json"),
            manifest,
            indent=2,
        )
        self._discard_recovery_payload(path, manifest)
        manifest["promotions"] = [
            {"id": str(row.get("id"))}
            for row in manifest.get("promotions") or ()
            if isinstance(row, dict) and row.get("id")
        ]
        manifest["skills"] = [
            {"skill_id": str(row.get("skill_id"))}
            for row in manifest.get("skills") or ()
            if isinstance(row, dict) and row.get("skill_id")
        ]
        atomic_write_json(
            str(path / "manifest.json"),
            manifest,
            indent=2,
        )

    def _ensure_recovery_capacity(self) -> None:
        if self.recovery_root.is_symlink():
            raise ValueError("memory-derived skill recovery root cannot be a symlink")
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.recovery_root, 0o700)
        self._recover_staging()
        recoveries = [
            item
            for item in self.recovery_root.iterdir()
            if (
                item.is_dir()
                and not item.is_symlink()
                and not item.name.startswith(".staging-")
            )
        ]
        active_recoveries = []
        for item in recoveries:
            try:
                manifest = json.loads(
                    (item / "manifest.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ValueError(
                    "memory-derived skill recovery ledger is unreadable"
                ) from exc
            if (
                manifest.get("state") == "restored"
                and float(manifest.get("expires_at") or 0) < time.time()
            ):
                shutil.rmtree(item)
                _sync_directories(self.recovery_root)
            elif manifest.get("state") in {
                "preparing",
                "prepared",
                "committed",
                "provider_restored",
                "restoring",
            }:
                active_recoveries.append(item)
        if len(active_recoveries) >= _RECOVERY_LIMIT:
            raise ValueError("too many pending memory forget recoveries")

    def prepare(
        self,
        owner: str,
        memory_ids: Iterable[object],
        preview_token: str,
        *,
        recovery_id: str | None = None,
        operation_kind: str = "forget",
        workspace_id: str | None = None,
        provider_owner: str | None = None,
    ) -> str:
        """Quarantine affected artifacts before the provider forget commits."""
        if operation_kind not in {"forget", "retention"}:
            raise ValueError("invalid memory-derived skill operation kind")
        workspace_id = self._workspace_id(workspace_id)
        provider_owner = memory_owner(
            provider_owner if provider_owner is not None else owner
        )
        local_id = self._validate_recovery_id(recovery_id or uuid.uuid4().hex)
        with locked(self.lock_target):
            self._recover_staging()
            final = self.recovery_root / local_id
            if final.is_dir():
                manifest = self._manifest(local_id)[1]
                if (
                    manifest.get("owner") == owner
                    and memory_owner(
                        manifest.get("provider_owner", manifest.get("owner"))
                    )
                    == provider_owner
                    and self._workspace_id(manifest.get("workspace_id"))
                    == workspace_id
                    and manifest.get("memory_ids") == self._memory_ids(memory_ids)
                    and manifest.get("preview_token") == preview_token
                    and manifest.get("operation_kind", "forget") == operation_kind
                    and manifest.get("state")
                    in {"prepared", "committed", "provider_restored"}
                ):
                    return local_id
                raise ValueError("memory-derived skill recovery identity conflicts")
            initial = self._snapshot(owner, memory_ids)
            skill_paths = sorted(
                row["_path"] for row in initial["_skill_rows"]
            )
            with ExitStack() as stack:
                for path in skill_paths:
                    stack.enter_context(locked(path))
                current = self._snapshot(owner, memory_ids)
                if current["token"] != preview_token:
                    raise ValueError("memory-derived skill closure changed")
                self._ensure_recovery_capacity()
                staging = Path(tempfile.mkdtemp(
                    prefix=".staging-",
                    dir=self.recovery_root,
                ))
                all_promotions = self.skills._load_promotions()
                promotion_ids = {
                    row["id"] for row in current["promotions"]
                }
                manifest = {
                    "version": 1,
                    "id": local_id,
                    "owner": owner,
                    "provider_owner": provider_owner,
                    "workspace_id": workspace_id,
                    "operation_kind": operation_kind,
                    "state": "preparing",
                    "created_at": time.time(),
                    "memory_ids": current["memory_ids"],
                    "preview_token": preview_token,
                    "promotions": current["promotions"],
                    "skills": current["skills"],
                }
                atomic_write_json(
                    str(staging / "manifest.json"),
                    manifest,
                    indent=2,
                )
                try:
                    for row in current["_skill_rows"]:
                        original = Path(row["_path"]).parent
                        relative = PurePosixPath(row["relative_dir"])
                        destination = staging / "skills" / Path(*relative.parts)
                        destination.mkdir(parents=True, exist_ok=True)
                        atomic_write_json(
                            str(original / _PLACEHOLDER),
                            {
                                "id": local_id,
                                "owner": owner,
                                "skill_id": row["skill_id"],
                            },
                            indent=2,
                        )
                        for entry in list(original.iterdir()):
                            if entry.name in {_SKILL_LOCK, _PLACEHOLDER}:
                                continue
                            os.replace(entry, destination / entry.name)
                        _sync_directories(
                            original,
                            destination,
                            destination.parent,
                        )
                    self.skills._save_promotions([
                        row
                        for row in all_promotions
                        if not (
                            row.get("owner") == owner
                            and row.get("id") in promotion_ids
                        )
                    ])
                    manifest["state"] = "prepared"
                    atomic_write_json(
                        str(staging / "manifest.json"),
                        manifest,
                        indent=2,
                    )
                    os.replace(staging, final)
                    _sync_directories(self.recovery_root)
                    return local_id
                except BaseException:
                    # Keep the only recovery copy if rollback itself fails.
                    # _recover_staging() will retry it on the next lifecycle
                    # entry instead of turning a transient rollback error into
                    # permanent skill loss.
                    rollback_source = final if final.is_dir() else staging
                    self._restore_local_path(rollback_source, manifest)
                    self.skills._save_promotions(all_promotions)
                    if rollback_source.exists():
                        shutil.rmtree(rollback_source)
                        _sync_directories(self.recovery_root)
                    raise

    @staticmethod
    def _expires_at(recover_until: object) -> float:
        value = str(recover_until or "").strip()
        try:
            return datetime.fromisoformat(
                value.replace("Z", "+00:00")
            ).timestamp()
        except ValueError as exc:
            raise ValueError("invalid provider recovery deadline") from exc

    def finalize(
        self,
        local_id: str,
        *,
        provider_tombstone_id: str,
        recover_until: object,
    ) -> bool:
        with locked(self.lock_target):
            path, manifest = self._manifest(local_id)
            if (
                manifest.get("state") == "committed"
                and manifest.get("provider_tombstone_id")
                == str(provider_tombstone_id)
            ):
                return True
            if (
                manifest.get("state") == "committed_irrecoverable"
                and manifest.get("provider_tombstone_id")
                == str(provider_tombstone_id)
            ):
                return False
            if manifest.get("state") != "prepared":
                raise ValueError("memory-derived skill recovery is not prepared")
            if recover_until in (None, ""):
                self._seal_irrecoverable(
                    path,
                    manifest,
                    provider_tombstone_id=provider_tombstone_id,
                )
                return False
            manifest.update({
                "state": "committed",
                "provider_tombstone_id": str(provider_tombstone_id),
                "recover_until": recover_until,
                "expires_at": self._expires_at(recover_until),
            })
            atomic_write_json(
                str(path / "manifest.json"),
                manifest,
                indent=2,
            )
            return True

    def seal_expired(
        self,
        local_id: str,
        *,
        owner: str,
        provider_tombstone_id: str,
    ) -> bool:
        """Discard an expired payload only after provider status is confirmed."""
        with locked(self.lock_target):
            path, manifest = self._manifest(local_id)
            if (
                manifest.get("owner") != owner
                or manifest.get("state") != "committed"
                or manifest.get("provider_tombstone_id")
                != str(provider_tombstone_id)
            ):
                return False
            if float(manifest.get("expires_at") or 0) >= time.time():
                return False
            self._seal_irrecoverable(
                path,
                manifest,
                provider_tombstone_id=provider_tombstone_id,
            )
            return True

    def _manifest(self, local_id: str) -> tuple[Path, dict]:
        local_id = self._validate_recovery_id(local_id)
        if self.recovery_root.is_symlink():
            raise ValueError("memory-derived skill recovery root cannot be a symlink")
        path = self.recovery_root / local_id
        if path.is_symlink() or path.resolve(strict=False).parent != self.recovery_root:
            raise ValueError("invalid memory-derived skill tombstone")
        try:
            manifest = json.loads(
                (path / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError("memory-derived skill tombstone not found") from exc
        if (
            not isinstance(manifest, dict)
            or manifest.get("id") != local_id
            or manifest.get("version") != 1
        ):
            raise ValueError("invalid memory-derived skill tombstone")
        return path, manifest

    def _validate_restore_contents(self, path: Path, manifest: dict) -> None:
        seen_skill_ids: set[str] = set()
        active_skill_ids = {
            (str(skill.owner or ""), str(skill.skill_id))
            for skill_path in self.skills._iter_skill_files() or ()
            if (skill := self.skills._read_skill(skill_path)) is not None
        }
        for row in manifest.get("skills") or ():
            relative = self._relative_dir(row)
            source = path / "skills" / Path(*relative.parts)
            destination = self._active_dir(row)
            if (
                source.is_symlink()
                or not source.is_dir()
                or path.resolve() not in source.resolve().parents
                or destination.is_symlink()
                or not destination.is_dir()
            ):
                raise ValueError("recovered skill path is unavailable")
            for root, directories, files in os.walk(source, followlinks=False):
                for name in [*directories, *files]:
                    if Path(root, name).is_symlink():
                        raise ValueError("recovered skill bundle contains a symlink")
            marker = self._read_marker(destination)
            if (
                marker.get("id") != manifest.get("id")
                or marker.get("owner") != manifest.get("owner")
                or marker.get("skill_id") != row.get("skill_id")
            ):
                raise ValueError("memory-derived skill placeholder is out of scope")
            unexpected = {
                entry.name
                for entry in destination.iterdir()
                if entry.name not in {_PLACEHOLDER, _SKILL_LOCK}
            }
            if unexpected:
                raise ValueError("recovered skill path now conflicts")

            skill_path = source / "SKILL.md"
            if skill_path.is_symlink() or not skill_path.is_file():
                raise ValueError("recovered skill has no safe SKILL.md")
            skill = self.skills._read_skill(str(skill_path))
            if (
                skill is None
                or str(skill.owner or "") != manifest.get("owner")
                or skill.skill_id != row.get("skill_id")
                or skill.revision != row.get("revision")
                or skill.content_hash != row.get("content_hash")
                or sorted(skill.source_memory_ids)
                != sorted(row.get("source_memory_ids") or ())
                or load_state(str(skill_path)).get("published")
                != row.get("published")
            ):
                raise ValueError("recovered skill identity no longer matches")
            if skill.skill_id in seen_skill_ids:
                raise ValueError("recovered skill identity is duplicated")
            seen_skill_ids.add(skill.skill_id)
            if (manifest.get("owner"), skill.skill_id) in active_skill_ids:
                raise ValueError("recovered skill identity now conflicts")

        active_promotions = self.skills._load_promotions()
        for restored in manifest.get("promotions") or ():
            owner = restored.get("owner")
            promotion_id = restored.get("id")
            if not isinstance(promotion_id, str) or not promotion_id:
                raise ValueError("recovered promotion has no stable identity")
            if any(
                row.get("owner") == owner and row.get("id") == promotion_id
                for row in active_promotions
            ):
                raise ValueError("promotion identity now conflicts with recovery")

    def preflight_restore(
        self,
        local_id: str,
        *,
        owner: str,
        provider_tombstone_id: str,
    ) -> dict:
        with locked(self.lock_target):
            path, manifest = self._manifest(local_id)
            if (
                manifest.get("owner") == owner
                and manifest.get("state") == "restored"
                and manifest.get("provider_tombstone_id")
                == str(provider_tombstone_id)
            ):
                return {
                    "provider_restored": True,
                    "local_restored": True,
                }
            if (
                manifest.get("owner") != owner
                or manifest.get("state")
                not in {"committed", "provider_restored", "restoring"}
                or manifest.get("provider_tombstone_id")
                != str(provider_tombstone_id)
            ):
                raise ValueError("memory-derived skill tombstone is out of scope")
            if (
                manifest.get("state") == "committed"
                and float(manifest.get("expires_at") or 0) < time.time()
            ):
                raise ValueError("memory-derived skill recovery window expired")
            if manifest.get("state") in {"committed", "provider_restored"}:
                self._validate_restore_contents(path, manifest)
            return {
                "provider_restored": manifest.get("state")
                in {"provider_restored", "restoring"},
                "local_restored": False,
                "provider_tombstone_id": manifest["provider_tombstone_id"],
            }

    def mark_provider_restored(
        self,
        local_id: str,
        *,
        owner: str,
        provider_tombstone_id: str,
    ) -> None:
        with locked(self.lock_target):
            path, manifest = self._manifest(local_id)
            if (
                manifest.get("owner") == owner
                and manifest.get("state") in {"provider_restored", "restoring", "restored"}
                and manifest.get("provider_tombstone_id")
                == str(provider_tombstone_id)
            ):
                return
            if (
                manifest.get("owner") != owner
                or manifest.get("state") != "committed"
                or manifest.get("provider_tombstone_id")
                != str(provider_tombstone_id)
            ):
                raise ValueError("memory-derived skill tombstone is out of scope")
            manifest["state"] = "provider_restored"
            manifest["provider_restored_at"] = time.time()
            atomic_write_json(
                str(path / "manifest.json"),
                manifest,
                indent=2,
            )

    def restore(
        self,
        local_id: str,
        *,
        owner: str,
        allow_prepared: bool = False,
    ) -> dict:
        """Restore one recovery after provider restore, or undo a failed commit."""
        with locked(self.lock_target):
            path, manifest = self._manifest(local_id)
            result = {
                "restored_promotion_ids": [
                    row.get("id") for row in manifest.get("promotions") or ()
                ],
                "restored_skill_ids": [
                    row.get("skill_id")
                    for row in manifest.get("skills") or ()
                ],
            }
            if allow_prepared:
                if (
                    manifest.get("owner") != owner
                    or manifest.get("state") != "prepared"
                ):
                    raise ValueError("memory-derived skill tombstone is out of scope")
                self._rollback_recovery(path, manifest)
                return result
            if (
                manifest.get("owner") == owner
                and manifest.get("state") == "restored"
            ):
                shutil.rmtree(path / "skills", ignore_errors=True)
                _sync_directories(path)
                return result
            if (
                manifest.get("owner") != owner
                or manifest.get("state") not in {"provider_restored", "restoring"}
            ):
                raise ValueError("memory-derived skill tombstone is out of scope")
            if manifest.get("state") == "provider_restored":
                self._validate_restore_contents(path, manifest)
                manifest["state"] = "restoring"
                manifest["restoring_at"] = time.time()
                atomic_write_json(
                    str(path / "manifest.json"),
                    manifest,
                    indent=2,
                )
            self._restore_local_path(
                path,
                manifest,
                remove_markers=False,
            )
            self._merge_promotions(manifest)
            for row in manifest.get("skills") or ():
                marker = self._active_dir(row) / _PLACEHOLDER
                if marker.exists():
                    marker.unlink()
                    _sync_directories(marker.parent)
            manifest["state"] = "restored"
            manifest["restored_at"] = time.time()
            atomic_write_json(
                str(path / "manifest.json"),
                manifest,
                indent=2,
            )
            shutil.rmtree(path / "skills", ignore_errors=True)
            _sync_directories(path)
            return result

    def pending_operations(self) -> list[dict]:
        with locked(self.lock_target):
            self._recover_staging()
            if not self.recovery_root.is_dir():
                return []
            pending = []
            for path in sorted(self.recovery_root.iterdir()):
                if (
                    not path.is_dir()
                    or path.is_symlink()
                    or path.name.startswith(".staging-")
                ):
                    continue
                try:
                    _, manifest = self._manifest(path.name)
                except ValueError as exc:
                    raise ValueError(
                        "memory-derived skill recovery ledger is invalid"
                    ) from exc
                if manifest.get("state") in {
                    "prepared",
                    "committed",
                    "provider_restored",
                    "restoring",
                }:
                    pending.append({
                        "id": path.name,
                        "owner": str(manifest.get("owner") or ""),
                        "provider_owner": memory_owner(
                            manifest.get(
                                "provider_owner",
                                manifest.get("owner"),
                            )
                        ),
                        "workspace_id": self._workspace_id(
                            manifest.get("workspace_id")
                        ),
                        "operation_kind": manifest.get("operation_kind", "forget"),
                        "state": manifest.get("state"),
                        "memory_ids": list(manifest.get("memory_ids") or ()),
                        "provider_tombstone_id": manifest.get(
                            "provider_tombstone_id"
                        ),
                    })
            return pending

    def operation_info(
        self,
        local_id: str,
        *,
        owner: str,
        workspace_id: str | None = None,
        provider_owner: str | None = None,
    ) -> dict | None:
        """Return the server-owned local binding for one lifecycle operation."""
        workspace_id = self._workspace_id(workspace_id)
        provider_owner = memory_owner(
            provider_owner if provider_owner is not None else owner
        )
        with locked(self.lock_target):
            try:
                _, manifest = self._manifest(local_id)
            except ValueError:
                return None
            if (
                manifest.get("owner") != owner
                or memory_owner(
                    manifest.get("provider_owner", manifest.get("owner"))
                )
                != provider_owner
                or self._workspace_id(manifest.get("workspace_id"))
                != workspace_id
            ):
                raise ValueError("memory-derived skill recovery is out of scope")
            return {
                "id": manifest["id"],
                "owner": manifest["owner"],
                "provider_owner": memory_owner(
                    manifest.get("provider_owner", manifest.get("owner"))
                ),
                "workspace_id": manifest.get("workspace_id"),
                "operation_kind": manifest.get("operation_kind", "forget"),
                "state": manifest.get("state"),
                "preview_token": manifest.get("preview_token"),
                "memory_ids": list(manifest.get("memory_ids") or ()),
                "provider_tombstone_id": manifest.get(
                    "provider_tombstone_id"
                ),
                "promotion_ids": [
                    row.get("id") for row in manifest.get("promotions") or ()
                ],
                "skill_ids": [
                    row.get("skill_id")
                    for row in manifest.get("skills") or ()
                ],
            }

    def recovery_info(
        self,
        local_id: str,
        *,
        owner: str,
        preview_token: str,
    ) -> dict | None:
        with locked(self.lock_target):
            try:
                _, manifest = self._manifest(local_id)
            except ValueError:
                return None
            if (
                manifest.get("owner") != owner
                or manifest.get("preview_token") != preview_token
                or manifest.get("state")
                not in {"prepared", "committed", "provider_restored", "restoring"}
            ):
                raise ValueError("memory-derived skill recovery is out of scope")
            return {
                "state": manifest["state"],
                "promotion_ids": [
                    row.get("id") for row in manifest.get("promotions") or ()
                ],
                "skill_ids": [
                    row.get("skill_id")
                    for row in manifest.get("skills") or ()
                ],
            }

    async def reconcile(
        self,
        memory_provider,
        *,
        owner: str = "",
        filter_owner: bool = False,
    ) -> dict[str, int]:
        counts = {"rolled_back": 0, "committed": 0, "restored": 0, "errors": 0}
        for row in await asyncio.to_thread(self.pending_operations):
            if filter_owner and row["owner"] != str(owner or ""):
                continue
            try:
                if row["operation_kind"] == "retention":
                    if row["state"] != "prepared":
                        raise ValueError(
                            "retention recovery has an invalid unfinished state"
                        )
                    status = await memory_provider.retention(
                        "status",
                        owner=row["provider_owner"],
                        workspace_id=row.get("workspace_id"),
                        operation_id=row["id"],
                    )
                    if status.get("state") == "absent":
                        await asyncio.to_thread(
                            self.restore,
                            row["id"],
                            owner=row["owner"],
                            allow_prepared=True,
                        )
                        counts["rolled_back"] += 1
                        continue
                    if status.get("state") != "committed":
                        raise ValueError(
                            "invalid provider retention operation state"
                        )
                    status_closure = status.get("closure")
                    if not isinstance(status_closure, dict) or sorted({
                        str(value)
                        for value in status_closure.get("curated_ids", ())
                        if str(value or "")
                    }) != sorted({
                        str(value)
                        for value in row.get("memory_ids", ())
                        if str(value or "")
                    }):
                        raise ValueError(
                            "provider retention closure does not match local recovery"
                        )
                    await asyncio.to_thread(
                        self.finalize,
                        row["id"],
                        provider_tombstone_id=f"retention:{row['id']}",
                        recover_until=None,
                    )
                    counts["committed"] += 1
                    continue
                status = await memory_provider.forget(
                    "status",
                    owner=row["provider_owner"],
                    workspace_id=row.get("workspace_id"),
                    operation_id=row["id"],
                )
                provider_state = status.get("state")
                local_state = row["state"]
                provider_tombstone_id = status.get("tombstone_id")
                if local_state == "prepared" and provider_state == "absent":
                    await asyncio.to_thread(
                        self.restore,
                        row["id"],
                        owner=row["owner"],
                        allow_prepared=True,
                    )
                    counts["rolled_back"] += 1
                    continue
                if provider_state not in {"committed", "restored"}:
                    raise ValueError("invalid provider forget operation state")
                if not isinstance(provider_tombstone_id, str):
                    raise ValueError("provider forget operation has no tombstone")
                status_closure = status.get("closure")
                if not isinstance(status_closure, dict) or sorted({
                    str(value)
                    for value in status_closure.get("curated_ids", ())
                    if str(value or "")
                }) != sorted({
                    str(value)
                    for value in row.get("memory_ids", ())
                    if str(value or "")
                }):
                    raise ValueError(
                        "provider forget closure does not match local recovery"
                    )

                if local_state == "prepared":
                    recoverable = await asyncio.to_thread(
                        self.finalize,
                        row["id"],
                        provider_tombstone_id=provider_tombstone_id,
                        recover_until=status.get("recover_until"),
                    )
                    if not recoverable:
                        counts["committed"] += 1
                        continue
                    local_state = "committed"
                elif row.get("provider_tombstone_id") != provider_tombstone_id:
                    raise ValueError(
                        "local and provider forget tombstones do not match"
                    )

                if local_state == "committed" and provider_state == "committed":
                    await asyncio.to_thread(
                        self.seal_expired,
                        row["id"],
                        owner=row["owner"],
                        provider_tombstone_id=provider_tombstone_id,
                    )
                    counts["committed"] += 1
                    continue
                if provider_state != "restored":
                    raise ValueError("local restore state is ahead of the provider")
                if local_state == "committed":
                    await asyncio.to_thread(
                        self.mark_provider_restored,
                        row["id"],
                        owner=row["owner"],
                        provider_tombstone_id=provider_tombstone_id,
                    )
                await asyncio.to_thread(
                    self.restore,
                    row["id"],
                    owner=row["owner"],
                )
                counts["restored"] += 1
            except Exception:
                counts["errors"] += 1
                logger.exception(
                    "Could not reconcile memory-derived skill forget %s",
                    row["id"],
                )
        return counts


class MemoryLifecycleCoordinator:
    """One destructive-memory boundary shared by REST, Agent, and MiMo."""

    _EMPTY_SKILL_TOKEN = hashlib.sha256(
        b"open-clank:no-memory-derived-artifacts:v1"
    ).hexdigest()

    def __init__(
        self,
        memory_provider,
        skills_manager=None,
        *,
        skill_forget: MemorySkillForgetCoordinator | None = None,
        skill_owner: str | None = None,
    ):
        self.provider = memory_provider
        # Memory providers canonicalize an anonymous local install to "local",
        # while its on-disk skills intentionally remain ownerless.  Keep those
        # two scope representations distinct at this one shared boundary.
        self.skill_owner = skill_owner
        self.skill_forget = (
            skill_forget
            if skill_forget is not None
            else (
                MemorySkillForgetCoordinator(skills_manager)
                if skills_manager is not None
                else None
            )
        )
        self._reconcile_tasks: set[asyncio.Task] = set()
        self._reconcile_task_owners: dict[asyncio.Task, str] = {}

    def _schedule_reconcile(self, owner: str) -> None:
        """Converge an ambiguous in-flight operation without an app restart."""
        if self.skill_forget is None:
            return
        reconcile_owner = self._artifact_owner(owner)
        owner_key = str(reconcile_owner or "").strip().casefold()

        async def settle() -> None:
            try:
                delay = max(
                    0.0,
                    float(
                        os.getenv(
                            "OPEN_CLANK_MEMORY_RECONCILE_DELAY_SECONDS",
                            "1",
                        )
                    ),
                )
            except ValueError:
                delay = 1.0
            if delay:
                await asyncio.sleep(delay)
            for attempt in range(5):
                counts = await self.reconcile(owner=reconcile_owner)
                if not int(counts.get("errors") or 0):
                    return
                await asyncio.sleep(min(2 ** attempt, 8))
            raise RuntimeError(
                "memory lifecycle background reconciliation did not converge"
            )

        task = asyncio.create_task(
            settle(),
            name="open-clank-memory-lifecycle-reconcile",
        )
        self._reconcile_tasks.add(task)
        if owner_key:
            self._reconcile_task_owners[task] = owner_key

        def completed(done: asyncio.Task) -> None:
            self._reconcile_tasks.discard(done)
            self._reconcile_task_owners.pop(done, None)
            try:
                error = done.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "Background memory lifecycle reconciliation failed",
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(completed)

    async def drain_owner_reconcile_tasks(self, owner: str) -> dict[str, object]:
        """Join every pending reconciliation writer for one exact owner."""

        owner_key = str(self._artifact_owner(owner) or "").strip().casefold()
        drained: set[asyncio.Task] = set()
        while True:
            pending = {
                task
                for task, task_owner in list(self._reconcile_task_owners.items())
                if task_owner == owner_key and not task.done()
            }
            if not pending:
                return {"owner": owner_key, "drained": len(drained)}
            drained.update(pending)
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))

    @staticmethod
    def _scope(owner: str, workspace_id: str | None = None) -> dict:
        return {
            "owner": memory_owner(owner),
            "workspace_id": str(workspace_id or "").strip() or chat_workspace(),
        }

    def _artifact_owner(self, owner: str) -> str:
        if self.skill_owner is not None:
            return str(self.skill_owner or "")
        return str(owner or "")

    @staticmethod
    def _normalized_closure(value: object) -> dict[str, list[str]]:
        closure = value if isinstance(value, dict) else {}
        return {
            key: sorted({
                str(item)
                for item in closure.get(key, ())
                if str(item or "")
            })
            for key in (
                "raw_ids",
                "candidate_ids",
                "curated_ids",
                "graph_node_ids",
            )
        }

    @classmethod
    def _require_same_closure(
        cls,
        expected: object,
        observed: object,
        *,
        operation: str,
    ) -> None:
        if cls._normalized_closure(expected) != cls._normalized_closure(observed):
            raise ValueError(f"memory provider {operation} closure changed")

    async def _derived(self, owner: str, closure: dict) -> dict:
        if self.skill_forget is None:
            return {
                "token": self._EMPTY_SKILL_TOKEN,
                "promotion_ids": [],
                "skill_ids": [],
            }
        return await asyncio.to_thread(
            self.skill_forget.preview,
            owner,
            closure.get("curated_ids") or (),
        )

    async def reconcile(self, *, owner: str | None = None) -> dict[str, int]:
        if self.skill_forget is None:
            return {
                "rolled_back": 0,
                "committed": 0,
                "restored": 0,
                "errors": 0,
            }
        if owner is None:
            reconcile_owner = str(self.skill_owner or "")
            filter_owner = self.skill_owner is not None
        else:
            reconcile_owner = self._artifact_owner(owner)
            filter_owner = True
        return await self.skill_forget.reconcile(
            self.provider,
            owner=reconcile_owner,
            filter_owner=filter_owner,
        )

    async def forget(
        self,
        action: str,
        *,
        owner: str,
        workspace_id: str | None = None,
        selector_kind=None,
        selector=None,
        preview_token=None,
        tombstone_id=None,
        operation_id=None,
    ) -> dict:
        if action not in {"preview", "commit", "status", "restore"}:
            raise ValueError("unsupported coordinated forget action")
        scope = self._scope(owner, workspace_id)
        artifact_owner = self._artifact_owner(owner)
        workspace_id = scope["workspace_id"]
        provider_selector = {
            **scope,
            "selector_kind": selector_kind,
            "selector": selector,
        }

        if action == "status":
            if not operation_id:
                raise ValueError("forget operation_id is required")
            return await self.provider.forget(
                "status",
                **scope,
                operation_id=operation_id,
            )

        if action == "preview":
            result = await self.provider.forget(
                "preview",
                **provider_selector,
            )
            provider_token = result.get("token")
            if not isinstance(provider_token, str) or not provider_token:
                raise ValueError("memory provider returned no forget preview token")
            closure = dict(result.get("closure") or {})
            derived = await self._derived(artifact_owner, closure)
            closure["promotion_ids"] = derived["promotion_ids"]
            closure["skill_ids"] = derived["skill_ids"]
            operation_id = uuid.uuid4().hex
            return {
                **result,
                "closure": closure,
                "operation_id": operation_id,
                "token": pack_forget_token(
                    "preview",
                    provider=provider_token,
                    skills=derived["token"],
                    operation=operation_id,
                ),
            }

        if action == "commit":
            tokens = unpack_forget_token(str(preview_token or ""), "preview")
            if set(tokens) != {"provider", "skills", "operation"}:
                raise ValueError("invalid composite memory forget token")
            coordinated_operation = tokens["operation"]
            if operation_id and operation_id != coordinated_operation:
                raise ValueError("memory forget operation binding changed")
            operation_id = coordinated_operation
            local_info = None
            if self.skill_forget is not None:
                local_info = await asyncio.to_thread(
                    self.skill_forget.operation_info,
                    operation_id,
                    owner=artifact_owner,
                    workspace_id=workspace_id,
                    provider_owner=scope["owner"],
                )
                if local_info is not None and (
                    local_info.get("operation_kind") != "forget"
                    or local_info.get("preview_token") != tokens["skills"]
                ):
                    raise ValueError(
                        "memory-derived skill recovery binding changed"
                    )
                if local_info is not None and local_info.get("state") in {
                    "provider_restored",
                    "restoring",
                    "restored",
                }:
                    raise ValueError(
                        "memory forget operation was already restored"
                    )

            try:
                status = await self.provider.forget(
                    "status",
                    **scope,
                    operation_id=operation_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                status = {}
            provider_state = status.get("state")
            if provider_state == "restored":
                raise ValueError("memory forget operation was already restored")

            if local_info is not None:
                if provider_state == "committed":
                    closure = dict(status.get("closure") or {})
                    if not closure:
                        raise ValueError(
                            "committed memory forget has no observable closure"
                        )
                else:
                    current = await self.provider.forget(
                        "preview",
                        **provider_selector,
                    )
                    if current.get("token") != tokens["provider"]:
                        raise ValueError("memory forget preview is stale")
                    closure = dict(current.get("closure") or {})
                derived = {
                    "token": tokens["skills"],
                    "promotion_ids": local_info["promotion_ids"],
                    "skill_ids": local_info["skill_ids"],
                }
                local_id = operation_id
            else:
                if provider_state == "committed":
                    closure = dict(status.get("closure") or {})
                    if not closure:
                        raise ValueError(
                            "committed memory forget has no observable closure"
                        )
                else:
                    current = await self.provider.forget(
                        "preview",
                        **provider_selector,
                    )
                    if current.get("token") != tokens["provider"]:
                        raise ValueError("memory forget preview is stale")
                    closure = dict(current.get("closure") or {})
                derived = await self._derived(artifact_owner, closure)
                if derived["token"] != tokens["skills"]:
                    raise ValueError("memory-derived skill closure changed")
                local_id = None
                if self.skill_forget is not None:
                    local_id = await asyncio.to_thread(
                        self.skill_forget.prepare,
                        artifact_owner,
                        closure.get("curated_ids") or (),
                        derived["token"],
                        recovery_id=operation_id,
                        workspace_id=workspace_id,
                        provider_owner=scope["owner"],
                    )

            try:
                committed = await self.provider.forget(
                    "commit",
                    **provider_selector,
                    preview_token=tokens["provider"],
                    operation_id=operation_id,
                )
            except (MemoryRequestRejectedError, ValueError):
                if local_info is None and local_id:
                    try:
                        await asyncio.to_thread(
                            self.skill_forget.restore,
                            local_id,
                            owner=artifact_owner,
                            allow_prepared=True,
                        )
                    except Exception:
                        logger.exception(
                            "Could not roll back rejected memory forget %s",
                            operation_id,
                        )
                raise
            except asyncio.CancelledError:
                self._schedule_reconcile(owner)
                raise
            except Exception:
                observed = None
                try:
                    observed = await self.provider.forget(
                        "status",
                        **scope,
                        operation_id=operation_id,
                    )
                except asyncio.CancelledError:
                    self._schedule_reconcile(owner)
                    raise
                except Exception:
                    pass
                if (
                    isinstance(observed, dict)
                    and observed.get("state") == "committed"
                    and observed.get("tombstone_id")
                ):
                    committed = observed
                else:
                    if (
                        local_id
                        and isinstance(observed, dict)
                        and observed.get("state") in {"absent", "restored"}
                    ):
                        await asyncio.to_thread(
                            self.skill_forget.restore,
                            local_id,
                            owner=artifact_owner,
                            allow_prepared=True,
                        )
                    if (
                        isinstance(observed, dict)
                        and observed.get("state") == "restored"
                    ):
                        raise ValueError(
                            "memory forget operation was already restored"
                        )
                    self._schedule_reconcile(owner)
                    raise

            provider_tombstone = committed.get("tombstone_id")
            if not isinstance(provider_tombstone, str) or not provider_tombstone:
                observed = None
                try:
                    observed = await self.provider.forget(
                        "status",
                        **scope,
                        operation_id=operation_id,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                if (
                    isinstance(observed, dict)
                    and observed.get("state") == "committed"
                    and isinstance(observed.get("tombstone_id"), str)
                ):
                    committed = observed
                    provider_tombstone = observed["tombstone_id"]
                else:
                    if (
                        local_id
                        and isinstance(observed, dict)
                        and observed.get("state") == "absent"
                    ):
                        await asyncio.to_thread(
                            self.skill_forget.restore,
                            local_id,
                            owner=artifact_owner,
                            allow_prepared=True,
                        )
                    raise ValueError(
                        "memory provider returned no forget tombstone"
                    )

            committed_closure = committed.get("closure")
            if committed_closure is not None:
                self._require_same_closure(
                    closure,
                    committed_closure,
                    operation="forget",
                )
            if local_id:
                recoverable = await asyncio.to_thread(
                    self.skill_forget.finalize,
                    local_id,
                    provider_tombstone_id=provider_tombstone,
                    recover_until=committed.get("recover_until"),
                )
                if not recoverable:
                    local_id = None

            closure = dict(committed.get("closure") or closure)
            closure["promotion_ids"] = derived["promotion_ids"]
            closure["skill_ids"] = derived["skill_ids"]
            tombstone_parts = {
                "provider": provider_tombstone,
                "operation": operation_id,
            }
            if local_id:
                tombstone_parts["skills"] = local_id
            return {
                **committed,
                "closure": closure,
                "tombstone_id": pack_forget_token(
                    "tombstone",
                    **tombstone_parts,
                ),
            }

        tombstones = unpack_forget_token(
            str(tombstone_id or ""),
            "tombstone",
        )
        if set(tombstones) not in (
            {"provider", "operation"},
            {"provider", "operation", "skills"},
        ):
            raise ValueError("invalid composite memory forget tombstone")
        operation_id = tombstones["operation"]
        local_info = None
        if self.skill_forget is not None:
            local_info = await asyncio.to_thread(
                self.skill_forget.operation_info,
                operation_id,
                owner=artifact_owner,
                workspace_id=workspace_id,
                provider_owner=scope["owner"],
            )
        if local_info is None:
            if tombstones.get("skills"):
                raise ValueError(
                    "memory-derived skill recovery does not exist"
                )
            preflight = {"provider_restored": False}
        else:
            if local_info.get("state") == "committed_irrecoverable":
                raise ValueError("memory-derived skill recovery is not recoverable")
            if (
                local_info.get("operation_kind") != "forget"
                or tombstones.get("skills") != operation_id
                or local_info.get("provider_tombstone_id")
                != tombstones["provider"]
            ):
                raise ValueError(
                    "composite memory forget tombstone is incomplete"
                )
            preflight = None

        status = await self.provider.forget(
            "status",
            **scope,
            operation_id=operation_id,
        )
        provider_state = status.get("state") if isinstance(status, dict) else None
        if provider_state in {"committed", "restored"} and (
            status.get("tombstone_id") != tombstones["provider"]
            or status.get("operation_id") not in (None, operation_id)
        ):
            raise ValueError("provider forget status binding changed")
        if provider_state == "restored":
            if local_info is not None:
                status_closure = status.get("closure")
                if not isinstance(status_closure, dict) or sorted({
                    str(value)
                    for value in status_closure.get("curated_ids", ())
                    if str(value or "")
                }) != sorted({
                    str(value)
                    for value in local_info.get("memory_ids", ())
                    if str(value or "")
                }):
                    raise ValueError(
                        "provider restore closure does not match local recovery"
                    )
                await asyncio.to_thread(
                    self.skill_forget.mark_provider_restored,
                    operation_id,
                    owner=artifact_owner,
                    provider_tombstone_id=tombstones["provider"],
                )
            preflight = {"provider_restored": True}
            restored = {**status, "restored": True}
        elif local_info is not None:
            preflight = await asyncio.to_thread(
                self.skill_forget.preflight_restore,
                operation_id,
                owner=artifact_owner,
                provider_tombstone_id=tombstones["provider"],
            )

        if not preflight.get("provider_restored"):
            try:
                provider_result = await self.provider.forget(
                    "restore",
                    **scope,
                    tombstone_id=tombstones["provider"],
                    operation_id=operation_id,
                )
            except (MemoryRequestRejectedError, ValueError):
                raise
            except asyncio.CancelledError:
                self._schedule_reconcile(owner)
                raise
            except Exception:
                # A lost restore response is ambiguous.  The durable status
                # record below is the only authority that may release local
                # derived artifacts.
                provider_result = {}
            try:
                status = await self.provider.forget(
                    "status",
                    **scope,
                    operation_id=operation_id,
                )
            except asyncio.CancelledError:
                self._schedule_reconcile(owner)
                raise
            except Exception:
                self._schedule_reconcile(owner)
                raise
            if (
                not isinstance(status, dict)
                or status.get("state") != "restored"
                or status.get("tombstone_id") != tombstones["provider"]
                or (
                    status.get("operation_id") not in (None, operation_id)
                )
            ):
                raise ValueError(
                    "memory provider did not confirm the forget restore"
                )
            if local_info is not None:
                status_closure = status.get("closure")
                if not isinstance(status_closure, dict) or sorted({
                    str(value)
                    for value in status_closure.get("curated_ids", ())
                    if str(value or "")
                }) != sorted({
                    str(value)
                    for value in local_info.get("memory_ids", ())
                    if str(value or "")
                }):
                    raise ValueError(
                        "provider restore closure does not match local recovery"
                    )
            restored = {
                **(provider_result if isinstance(provider_result, dict) else {}),
                **status,
                "restored": True,
            }
            if local_info is not None:
                await asyncio.to_thread(
                    self.skill_forget.mark_provider_restored,
                    operation_id,
                    owner=artifact_owner,
                    provider_tombstone_id=tombstones["provider"],
                )
        elif provider_state != "restored":
            raise ValueError("local restore state is ahead of the provider")
        local = {}
        if local_info is not None:
            local = await asyncio.to_thread(
                self.skill_forget.restore,
                operation_id,
                owner=artifact_owner,
            )
        return {
            **restored,
            **local,
            "tombstone_id": tombstone_id,
        }

    async def delete(
        self,
        memory_id: str,
        *,
        owner: str,
        workspace_id: str | None = None,
    ) -> dict | None:
        preview = await self.forget(
            "preview",
            owner=owner,
            workspace_id=workspace_id,
            selector_kind="record_id",
            selector=memory_id,
        )
        closure = preview.get("closure") or {}
        if not any(
            closure.get(key)
            for key in (
                "raw_ids",
                "candidate_ids",
                "curated_ids",
                "graph_node_ids",
            )
        ):
            return None
        return await self.forget(
            "commit",
            owner=owner,
            workspace_id=workspace_id,
            selector_kind="record_id",
            selector=memory_id,
            preview_token=preview["token"],
        )

    async def retention(
        self,
        action: str,
        *,
        owner: str,
        workspace_id: str | None = None,
        preview_token: str | None = None,
        operation_id: str | None = None,
    ) -> dict:
        """Coordinate retention preview/commit/status with derived artifacts."""
        if action not in {"preview_expire", "expire", "status"}:
            raise ValueError("unsupported coordinated retention action")
        scope = self._scope(owner, workspace_id)
        artifact_owner = self._artifact_owner(owner)
        workspace_id = scope["workspace_id"]

        if action == "status":
            if not operation_id:
                raise ValueError("retention operation_id is required")
            return await self.provider.retention(
                "status",
                **scope,
                operation_id=operation_id,
            )

        if action == "preview_expire":
            preview = await self.provider.retention(
                "preview_expire",
                **scope,
            )
            provider_token = preview.get("token")
            closure = dict(preview.get("closure") or {})
            if not isinstance(provider_token, str) or not provider_token:
                raise ValueError(
                    "memory provider returned no retention preview token"
                )
            derived = await self._derived(artifact_owner, closure)
            coordinated_operation = uuid.uuid4().hex
            closure["promotion_ids"] = derived["promotion_ids"]
            closure["skill_ids"] = derived["skill_ids"]
            return {
                **preview,
                "closure": closure,
                "operation_id": coordinated_operation,
                "token": pack_forget_token(
                    "retention-preview",
                    provider=provider_token,
                    skills=derived["token"],
                    operation=coordinated_operation,
                ),
            }

        tokens = unpack_forget_token(
            str(preview_token or ""),
            "retention-preview",
        )
        if set(tokens) != {"provider", "skills", "operation"}:
            raise ValueError("invalid composite memory retention token")
        coordinated_operation = tokens["operation"]
        if operation_id and operation_id != coordinated_operation:
            raise ValueError("memory retention operation binding changed")
        operation_id = coordinated_operation

        local_info = None
        if self.skill_forget is not None:
            local_info = await asyncio.to_thread(
                self.skill_forget.operation_info,
                operation_id,
                owner=artifact_owner,
                workspace_id=workspace_id,
                provider_owner=scope["owner"],
            )
            if local_info is not None and (
                local_info.get("operation_kind") != "retention"
                or local_info.get("preview_token") != tokens["skills"]
                or local_info.get("state")
                not in {"prepared", "committed_irrecoverable"}
            ):
                raise ValueError(
                    "memory-derived retention recovery binding changed"
                )

        try:
            status = await self.provider.retention(
                "status",
                **scope,
                operation_id=operation_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            status = {}
        provider_state = status.get("state")
        if (
            local_info is not None
            and local_info.get("state") == "committed_irrecoverable"
            and provider_state != "committed"
        ):
            raise ValueError(
                "local retention ledger is ahead of provider status"
            )

        if provider_state == "committed":
            closure = dict(status.get("closure") or {})
            if not closure:
                raise ValueError(
                    "committed memory retention has no observable closure"
                )
        else:
            current = await self.provider.retention(
                "preview_expire",
                **scope,
            )
            if current.get("token") != tokens["provider"]:
                raise ValueError("memory retention preview is stale")
            closure = dict(current.get("closure") or {})

        if local_info is None:
            derived = await self._derived(artifact_owner, closure)
            if derived["token"] != tokens["skills"]:
                raise ValueError("memory-derived retention closure changed")
            local_id = None
            if self.skill_forget is not None:
                local_id = await asyncio.to_thread(
                    self.skill_forget.prepare,
                    artifact_owner,
                    closure.get("curated_ids") or (),
                    derived["token"],
                    recovery_id=operation_id,
                    operation_kind="retention",
                    workspace_id=workspace_id,
                    provider_owner=scope["owner"],
                )
        else:
            local_id = operation_id
            derived = {
                "token": tokens["skills"],
                "promotion_ids": local_info["promotion_ids"],
                "skill_ids": local_info["skill_ids"],
            }

        try:
            expired = await self.provider.retention(
                "expire",
                **scope,
                preview_token=tokens["provider"],
                operation_id=operation_id,
            )
        except (MemoryRequestRejectedError, ValueError):
            if local_info is None and local_id:
                try:
                    await asyncio.to_thread(
                        self.skill_forget.restore,
                        local_id,
                        owner=artifact_owner,
                        allow_prepared=True,
                    )
                except Exception:
                    logger.exception(
                        "Could not roll back rejected retention %s",
                        operation_id,
                    )
            raise
        except asyncio.CancelledError:
            self._schedule_reconcile(owner)
            raise
        except Exception:
            observed = None
            try:
                observed = await self.provider.retention(
                    "status",
                    **scope,
                    operation_id=operation_id,
                )
            except asyncio.CancelledError:
                self._schedule_reconcile(owner)
                raise
            except Exception:
                pass
            if (
                isinstance(observed, dict)
                and observed.get("state") == "committed"
            ):
                expired = observed
            else:
                if (
                    local_info is None
                    and local_id
                    and isinstance(observed, dict)
                    and observed.get("state") == "absent"
                ):
                    await asyncio.to_thread(
                        self.skill_forget.restore,
                        local_id,
                        owner=artifact_owner,
                        allow_prepared=True,
                    )
                self._schedule_reconcile(owner)
                raise
        expired_closure = dict(expired.get("closure") or expired)
        self._require_same_closure(
            closure,
            expired_closure,
            operation="retention",
        )
        if local_id:
            await asyncio.to_thread(
                self.skill_forget.finalize,
                local_id,
                provider_tombstone_id=f"retention:{operation_id}",
                recover_until=None,
            )
        expired_closure["promotion_ids"] = derived["promotion_ids"]
        expired_closure["skill_ids"] = derived["skill_ids"]
        return {
            "operation_id": operation_id,
            "state": "committed",
            "closure": expired_closure,
        }

    async def expire_retention(
        self,
        *,
        owner: str,
        workspace_id: str | None = None,
    ) -> dict:
        """Convenience one-shot API used by the REST action."""
        preview = await self.retention(
            "preview_expire",
            owner=owner,
            workspace_id=workspace_id,
        )
        committed = await self.retention(
            "expire",
            owner=owner,
            workspace_id=workspace_id,
            preview_token=preview["token"],
            operation_id=preview["operation_id"],
        )
        return committed["closure"]

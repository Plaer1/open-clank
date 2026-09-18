"""Exact owner lifecycle for normalized provider-operation state.

The authentication routes span several persistence domains that are not part
of ``core.database.Base``.  Keeping their rename/purge calls in one small
coordinator makes omissions visible and gives pre-authentication rename steps
deterministic compensation.  Final account deletion still uses a tombstone
owner: every purge below is idempotent, so a retained tombstone can be retried
without ever making the deleted username authoritative again.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from core.atomic_io import atomic_write_json
from core.database import SessionLocal
from core.operation_models import AccountLifecycleOperation, operation_utcnow
from services.memory.skill_lifecycle import locked
from src.constants import DATA_DIR
from src.default_persona import (
    purge_default_persona_owner,
    rename_default_persona_owner,
)
from src.openclank.artifacts import ArtifactStore
from src.openclank.operation_journal import OperationJournalStore
from src.openclank.provider_store import ProviderStore


def _owner(value: Any) -> str:
    owner = str(value or "").strip().lower()
    if not owner or "\x00" in owner:
        raise AccountOwnerLifecycleError("account lifecycle owner is required")
    return owner


def _changed(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_changed(item) for item in value.values())
    return bool(value)


def _scrub_recovery_paths(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _scrub_recovery_paths(item)
            for key, item in value.items()
            if "path" not in str(key).lower()
        }
    if isinstance(value, list):
        return [_scrub_recovery_paths(item) for item in value]
    return value


@dataclass(frozen=True)
class AccountOwnerLifecycleReceipt:
    source_owner: str
    target_owner: str | None
    stores: Mapping[str, Any]


class AccountOwnerLifecycleError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        receipt: AccountOwnerLifecycleReceipt | None = None,
        failed_store: str | None = None,
        rollback_errors: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.receipt = receipt
        self.failed_store = failed_store
        self.rollback_errors = dict(rollback_errors or {})


class AccountOwnerLifecycle:
    """Coordinate the owner stores omitted by the generic SQL owner sweep."""

    def __init__(
        self,
        *,
        provider_store: ProviderStore,
        operation_journal: OperationJournalStore,
        artifact_store: ArtifactStore,
        preset_manager: Any,
        operation_path: Path | None = None,
        clock=time.time,
    ) -> None:
        self.provider_store = provider_store
        self.operation_journal = operation_journal
        self.artifact_store = artifact_store
        self.preset_manager = preset_manager
        self.operation_path = Path(
            operation_path or (Path(DATA_DIR) / ".account-lifecycle" / "operations.json")
        )
        self.clock = clock
        self.coordinator_id = uuid.uuid4().hex
        self._session_factory = operation_journal._session_factory

    @staticmethod
    def _operation_dict(row: AccountLifecycleOperation) -> dict[str, Any]:
        return {
            "version": 1,
            "operation_id": row.id,
            "kind": row.kind,
            "actor_account_id": row.actor_account_id,
            "source_owner": row.source_owner,
            "target_owner": row.target_owner,
            "subject_id": row.account_id,
            "tombstone_owner": row.tombstone_owner,
            "state": row.state,
            "revision": int(row.revision),
            "manifest": dict(row.manifest or {}),
            "steps": dict(row.steps or {}),
            "receipt": dict(row.receipt or {}),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    def _load_operations(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.operation_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AccountOwnerLifecycleError(
                "account lifecycle operation journal is unreadable"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise AccountOwnerLifecycleError(
                "account lifecycle operation journal has an unsupported schema"
            )
        operations = payload.get("operations")
        if not isinstance(operations, dict):
            raise AccountOwnerLifecycleError(
                "account lifecycle operation journal is malformed"
            )
        return {str(key): dict(value) for key, value in operations.items()}

    def _save_operations(self, operations: Mapping[str, Mapping[str, Any]]) -> None:
        self.operation_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(
            str(self.operation_path),
            {"version": 1, "operations": dict(operations)},
            indent=2,
        )

    def begin_delete_operation(
        self,
        *,
        actor: str,
        source_owner: str,
        subject_id: str,
        tombstone_owner: str,
    ) -> dict[str, Any]:
        source_owner = _owner(source_owner)
        tombstone_owner = _owner(tombstone_owner)
        subject_id = str(subject_id or "").strip()
        if not subject_id:
            raise AccountOwnerLifecycleError("immutable account subject is required")
        operation_id = "account-delete-" + uuid.uuid4().hex
        manifest = {
            "source": self.owner_inventory(source_owner),
            "tombstone": self.owner_inventory(tombstone_owner),
        }
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                requested_owners = {source_owner, tombstone_owner}
                for active in db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.active_account_id.is_not(None)
                ).all():
                    active_owners = {
                        _owner(value)
                        for value in (
                            active.source_owner,
                            active.target_owner,
                            active.tombstone_owner,
                        )
                        if value
                    }
                    if requested_owners.intersection(active_owners):
                        raise AccountOwnerLifecycleError(
                            "account owner is already reserved by an active lifecycle operation"
                        )
                row = AccountLifecycleOperation(
                    id=operation_id,
                    account_id=subject_id,
                    actor_account_id=_owner(actor),
                    kind="delete",
                    source_owner=source_owner,
                    tombstone_owner=tombstone_owner,
                    state="prepared",
                    active_account_id=subject_id,
                    manifest=manifest,
                    steps={},
                    receipt={},
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                record = self._operation_dict(row)
            except Exception as exc:
                db.rollback()
                raise AccountOwnerLifecycleError(
                    "account already has an active lifecycle operation"
                ) from exc
            finally:
                db.close()
        return record

    def begin_rename_operation(
        self,
        *,
        actor_account_id: str,
        source_owner: str,
        target_owner: str,
        subject_id: str,
    ) -> dict[str, Any]:
        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        subject_id = str(subject_id or "").strip()
        if not subject_id:
            raise AccountOwnerLifecycleError("immutable account subject is required")
        operation_id = "account-rename-" + uuid.uuid4().hex
        manifest = {
            "source": self.owner_inventory(source_owner),
            "target": self.owner_inventory(target_owner),
        }
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                requested_owners = {source_owner, target_owner}
                for active in db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.active_account_id.is_not(None)
                ).all():
                    active_owners = {
                        _owner(value)
                        for value in (
                            active.source_owner,
                            active.target_owner,
                            active.tombstone_owner,
                        )
                        if value
                    }
                    if requested_owners.intersection(active_owners):
                        raise AccountOwnerLifecycleError(
                            "account owner is already reserved by an active lifecycle operation"
                        )
                row = AccountLifecycleOperation(
                    id=operation_id,
                    account_id=subject_id,
                    actor_account_id=str(actor_account_id or "").strip(),
                    kind="rename",
                    source_owner=source_owner,
                    target_owner=target_owner,
                    state="prepared",
                    active_account_id=subject_id,
                    manifest=manifest,
                    steps={},
                    receipt={},
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                record = self._operation_dict(row)
            except Exception as exc:
                db.rollback()
                raise AccountOwnerLifecycleError(
                    "account already has an active lifecycle operation"
                ) from exc
            finally:
                db.close()
        return record

    def get_delete_operation(self, operation_id: str) -> dict[str, Any]:
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, str(operation_id or "").strip())
                record = self._operation_dict(row) if row is not None else None
            finally:
                db.close()
        if not isinstance(record, dict) or record.get("kind") != "delete":
            raise AccountOwnerLifecycleError("account deletion operation was not found")
        return dict(record)

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, str(operation_id or "").strip())
                if row is None:
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                return self._operation_dict(row)
            finally:
                db.close()

    def list_active_operations(self) -> list[dict[str, Any]]:
        """Return content-silent recovery handles for unfinished sagas."""

        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                rows = (
                    db.query(AccountLifecycleOperation)
                    .filter(AccountLifecycleOperation.active_account_id.is_not(None))
                    .order_by(
                        AccountLifecycleOperation.updated_at.desc(),
                        AccountLifecycleOperation.id,
                    )
                    .all()
                )
                return [
                    {
                        "operation_id": row.id,
                        "kind": row.kind,
                        "source_owner": row.source_owner,
                        "target_owner": row.target_owner,
                        "tombstone_owner": row.tombstone_owner,
                        "state": row.state,
                        "revision": int(row.revision),
                        "created_at": (
                            row.created_at.isoformat() if row.created_at else None
                        ),
                        "updated_at": (
                            row.updated_at.isoformat() if row.updated_at else None
                        ),
                    }
                    for row in rows
                ]
            finally:
                db.close()

    def owner_has_active_operation(self, owner: str) -> bool:
        owner = _owner(owner)
        db = self._session_factory()
        try:
            return db.query(AccountLifecycleOperation.id).filter(
                AccountLifecycleOperation.active_account_id.is_not(None),
                (
                    (AccountLifecycleOperation.source_owner == owner)
                    | (AccountLifecycleOperation.target_owner == owner)
                    | (AccountLifecycleOperation.tombstone_owner == owner)
                ),
            ).first() is not None
        finally:
            db.close()

    @staticmethod
    def _process_running(pid: Any) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    def claim_delete_operation(
        self,
        operation_id: str,
        *,
        force_foreign_takeover: bool = False,
    ) -> dict[str, Any]:
        return self.claim_operation(
            operation_id,
            expected_kind="delete",
            force_foreign_takeover=force_foreign_takeover,
        )

    def claim_operation(
        self,
        operation_id: str,
        *,
        expected_kind: str | None = None,
        force_foreign_takeover: bool = False,
    ) -> dict[str, Any]:
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None or (expected_kind and row.kind != expected_kind):
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                if row.state in {"aborted", "complete"} or row.active_account_id is None:
                    raise AccountOwnerLifecycleError(
                        "account lifecycle operation is terminal and cannot be resumed"
                    )
                receipt = dict(row.receipt or {})
                claim = receipt.get("claim")
                if isinstance(claim, dict):
                    foreign_host = bool(
                        claim.get("host") and claim.get("host") != socket.gethostname()
                    )
                    # A PID is meaningful only on its originating host.  An
                    # administrator may explicitly recover an operation whose
                    # old host will never return, but ``force`` must never
                    # steal a live same-host handler.
                    live_same_host = (
                        not foreign_host
                        and self._process_running(claim.get("pid"))
                    )
                    if live_same_host or (
                        foreign_host and not force_foreign_takeover
                    ):
                        raise AccountOwnerLifecycleError(
                            "account lifecycle operation is already in progress"
                        )
                receipt["claim"] = {
                    "coordinator_id": self.coordinator_id,
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    # Bind every later CAS write to this specific request, not
                    # merely to a process-wide coordinator or PID.
                    "token": uuid.uuid4().hex,
                }
                expected = int(row.revision)
                changed = db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.id == operation_id,
                    AccountLifecycleOperation.revision == expected,
                ).update(
                    {
                        AccountLifecycleOperation.receipt: receipt,
                        AccountLifecycleOperation.revision: expected + 1,
                        AccountLifecycleOperation.updated_at: operation_utcnow(),
                    },
                    synchronize_session=False,
                )
                if changed != 1:
                    raise AccountOwnerLifecycleError("account lifecycle revision conflict")
                db.commit()
                row = db.get(AccountLifecycleOperation, operation_id)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def _require_claim(
        row: AccountLifecycleOperation,
        claim_token: str | None,
    ) -> None:
        claim = dict((row.receipt or {}).get("claim") or {})
        if not claim:
            # Unit-level and legacy callers may mutate an operation that has
            # never been claimed. Durable routes claim first and are thereafter
            # required to present the request-specific token.
            if claim_token:
                raise AccountOwnerLifecycleError(
                    "account lifecycle operation is not claimed"
                )
            return
        expected = str(claim.get("token") or "")
        if not expected or not claim_token or not secrets.compare_digest(
            expected,
            str(claim_token),
        ):
            raise AccountOwnerLifecycleError(
                "account lifecycle claim no longer belongs to this request"
            )

    def set_operation_state(
        self,
        operation_id: str,
        state: str,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        allowed = {
            "prepared",
            "staging",
            "ready_to_commit",
            "auth_committing",
            "committed",
            "converging",
            "purging",
            "partial",
            "complete",
            "aborted",
        }
        if state not in allowed:
            raise AccountOwnerLifecycleError("invalid account lifecycle state")
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                self._require_claim(row, claim_token)
                row.state = state
                row.revision = int(row.revision) + 1
                row.updated_at = operation_utcnow()
                db.commit()
                db.refresh(row)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def freeze_operation_manifest(
        self,
        operation_id: str,
        manifest: Mapping[str, Any],
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Persist the complete read-only closure before the first effect."""
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                self._require_claim(row, claim_token)
                if row.steps:
                    raise AccountOwnerLifecycleError(
                        "account lifecycle manifest cannot change after effects begin"
                    )
                row.manifest = dict(manifest)
                row.revision = int(row.revision) + 1
                row.updated_at = operation_utcnow()
                db.commit()
                db.refresh(row)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def abort_operation(
        self,
        operation_id: str,
        exc: BaseException,
        *,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Terminally release a mutation-free operation after preflight fails."""
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                self._require_claim(row, claim_token)
                if row.steps:
                    raise AccountOwnerLifecycleError(
                        "an operation with started effects cannot be aborted"
                    )
                row.state = "aborted"
                row.active_account_id = None
                row.completed_at = operation_utcnow()
                row.receipt = {
                    "error": " ".join(str(exc).split())[:300]
                    or exc.__class__.__name__
                }
                row.revision = int(row.revision) + 1
                row.updated_at = operation_utcnow()
                db.commit()
                db.refresh(row)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def start_operation_step(
        self,
        operation_id: str,
        step: str,
        *,
        manifest: Mapping[str, Any] | None = None,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        """Persist an applying marker before invoking an external effect."""
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError("account lifecycle operation was not found")
                self._require_claim(row, claim_token)
                expected = int(row.revision)
                steps = dict(row.steps or {})
                prior = dict(steps.get(str(step)) or {})
                steps[str(step)] = {
                    "state": "applying",
                    "attempts": int(prior.get("attempts") or 0) + 1,
                }
                operation_manifest = dict(row.manifest or {})
                if manifest is not None:
                    operation_manifest[str(step)] = dict(manifest)
                changed = db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.id == operation_id,
                    AccountLifecycleOperation.revision == expected,
                ).update(
                    {
                        AccountLifecycleOperation.steps: steps,
                        AccountLifecycleOperation.manifest: operation_manifest,
                        AccountLifecycleOperation.revision: expected + 1,
                        AccountLifecycleOperation.updated_at: operation_utcnow(),
                    },
                    synchronize_session=False,
                )
                if changed != 1:
                    raise AccountOwnerLifecycleError("account lifecycle revision conflict")
                db.commit()
                row = db.get(AccountLifecycleOperation, operation_id)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def checkpoint_delete_operation(
        self,
        operation_id: str,
        step: str,
        receipt: Any = True,
        *,
        complete: bool = False,
        claim_token: str | None = None,
    ) -> dict[str, Any]:
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError("account deletion operation was not found")
                self._require_claim(row, claim_token)
                steps = dict(row.steps or {})
                expected = int(row.revision)
                prior = dict(steps.get(str(step)) or {})
                attempts = int(prior.get("attempts") or 0)
                if prior.get("state") != "applying":
                    attempts += 1
                steps[str(step)] = {"state": "applied", "attempts": attempts, "receipt": receipt}
                stored_receipt = dict(row.receipt or {})
                if complete:
                    state = "complete"
                elif row.kind == "rename":
                    auth_step = steps.get("auth_renamed")
                    state = (
                        "converging"
                        if isinstance(auth_step, Mapping)
                        and auth_step.get("state") == "applied"
                        else row.state
                    )
                else:
                    auth_step = steps.get("auth_deleted")
                    state = (
                        "purging"
                        if isinstance(auth_step, Mapping)
                        and auth_step.get("state") == "applied"
                        else "staging"
                    )
                if complete:
                    stored_receipt.pop("claim", None)
                values = {
                    AccountLifecycleOperation.steps: steps,
                    AccountLifecycleOperation.receipt: stored_receipt,
                    AccountLifecycleOperation.state: state,
                    AccountLifecycleOperation.revision: expected + 1,
                    AccountLifecycleOperation.updated_at: operation_utcnow(),
                }
                if complete:
                    values[AccountLifecycleOperation.active_account_id] = None
                    values[AccountLifecycleOperation.completed_at] = operation_utcnow()
                    values[AccountLifecycleOperation.manifest] = _scrub_recovery_paths(
                        dict(row.manifest or {})
                    )
                changed = db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.id == operation_id,
                    AccountLifecycleOperation.revision == expected,
                ).update(values, synchronize_session=False)
                if changed != 1:
                    raise AccountOwnerLifecycleError("account lifecycle revision conflict")
                db.commit()
                row = db.get(AccountLifecycleOperation, operation_id)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def fail_delete_operation(
        self,
        operation_id: str,
        exc: BaseException,
        *,
        claim_token: str | None = None,
    ) -> None:
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    return
                self._require_claim(row, claim_token)
                receipt = dict(row.receipt or {})
                receipt.pop("claim", None)
                receipt["error"] = (
                    " ".join(str(exc).split())[:300] or exc.__class__.__name__
                )
                row.receipt = receipt
                row.state = "partial"
                row.revision = int(row.revision) + 1
                row.updated_at = operation_utcnow()
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def rewind_operation_steps(
        self,
        operation_id: str,
        step_names: list[str] | tuple[str, ...],
        exc: BaseException,
        *,
        claim_token: str,
    ) -> dict[str, Any]:
        """Release a claim after verified pre-commit compensation.

        The frozen manifest remains authoritative. Restored effects are
        removed so a later resume replays them instead of mistaking a
        compensated step for an applied one.
        """
        with locked(str(self.operation_path)):
            db = self._session_factory()
            try:
                row = db.get(AccountLifecycleOperation, operation_id)
                if row is None:
                    raise AccountOwnerLifecycleError(
                        "account lifecycle operation was not found"
                    )
                self._require_claim(row, claim_token)
                expected = int(row.revision)
                steps = dict(row.steps or {})
                for name in step_names:
                    steps.pop(str(name), None)
                receipt = dict(row.receipt or {})
                receipt.pop("claim", None)
                receipt["error"] = (
                    " ".join(str(exc).split())[:300]
                    or exc.__class__.__name__
                )
                changed = db.query(AccountLifecycleOperation).filter(
                    AccountLifecycleOperation.id == operation_id,
                    AccountLifecycleOperation.revision == expected,
                ).update(
                    {
                        AccountLifecycleOperation.steps: steps,
                        AccountLifecycleOperation.receipt: receipt,
                        AccountLifecycleOperation.state: "partial",
                        AccountLifecycleOperation.revision: expected + 1,
                        AccountLifecycleOperation.updated_at: operation_utcnow(),
                    },
                    synchronize_session=False,
                )
                if changed != 1:
                    raise AccountOwnerLifecycleError(
                        "account lifecycle revision conflict"
                    )
                db.commit()
                row = db.get(AccountLifecycleOperation, operation_id)
                return self._operation_dict(row)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        """Inventory every coordinated store without returning user content."""
        owner = _owner(owner)
        personas = self.preset_manager.presets.get("default_personas") or {}
        persona_present = isinstance(personas, dict) and owner in personas
        stores = {
            "provider": self.provider_store.owner_inventory(owner),
            "operation_journal": self.operation_journal.owner_inventory(owner),
            "artifacts": self.artifact_store.owner_inventory(owner),
            "default_persona": {"count": int(persona_present)},
        }
        material = json.dumps(stores, sort_keys=True, separators=(",", ":"))
        return {
            "owner": owner,
            "count": sum(int(item.get("count") or 0) for item in stores.values()),
            "stores": stores,
            "fingerprint": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }

    def reconcile_rename(
        self,
        source_owner: str,
        target_owner: str,
    ) -> AccountOwnerLifecycleReceipt:
        """Resume a possibly crashed rename using source/target inventories."""
        source_owner = _owner(source_owner)
        target_owner = _owner(target_owner)
        stores: dict[str, Any] = {}
        definitions = (
            (
                "operation_journal",
                self.operation_journal.owner_inventory,
                self.operation_journal.rename_owner,
            ),
            ("artifacts", self.artifact_store.owner_inventory, self.artifact_store.rename_owner),
            (
                "default_persona",
                lambda owner: {
                    "count": int(
                        owner in (self.preset_manager.presets.get("default_personas") or {})
                    )
                },
                lambda old, new: int(
                    bool(
                        rename_default_persona_owner(
                            old, new, preset_manager=self.preset_manager
                        )
                    )
                ),
            ),
            # Provider remains last because owner-bound leases and replay keys
            # are deliberately invalidated by its rename.
            ("provider", self.provider_store.owner_inventory, self.provider_store.rename_owner),
        )
        for label, inventory, rename in definitions:
            source = inventory(source_owner)
            target = inventory(target_owner)
            source_count = int(source.get("count") or 0)
            target_count = int(target.get("count") or 0)
            if source_count and target_count:
                receipt = AccountOwnerLifecycleReceipt(source_owner, target_owner, stores)
                raise AccountOwnerLifecycleError(
                    f"account owner reconciliation conflicts in {label}",
                    receipt=receipt,
                    failed_store=label,
                )
            if source_count:
                stores[label] = rename(source_owner, target_owner)
            else:
                stores[label] = {"already_applied": bool(target_count)}
        return AccountOwnerLifecycleReceipt(source_owner, target_owner, stores)

    def rename_owner(
        self,
        old_owner: str,
        new_owner: str,
    ) -> AccountOwnerLifecycleReceipt:
        old_owner = _owner(old_owner)
        new_owner = _owner(new_owner)
        if old_owner == new_owner:
            return AccountOwnerLifecycleReceipt(old_owner, new_owner, {})

        completed: list[tuple[str, Any, Any]] = []
        stores: dict[str, Any] = {}
        current_store = "operation_journal"
        try:
            result = self.operation_journal.rename_owner(old_owner, new_owner)
            stores[current_store] = result
            completed.append(
                (
                    current_store,
                    result,
                    lambda: self.operation_journal.rename_owner(new_owner, old_owner),
                )
            )

            current_store = "artifacts"
            result = self.artifact_store.rename_owner(old_owner, new_owner)
            stores[current_store] = result
            completed.append(
                (
                    current_store,
                    result,
                    lambda: self.artifact_store.rename_owner(new_owner, old_owner),
                )
            )

            current_store = "default_persona"
            result = rename_default_persona_owner(
                old_owner,
                new_owner,
                preset_manager=self.preset_manager,
            )
            stores[current_store] = int(bool(result))
            completed.append(
                (
                    current_store,
                    result,
                    lambda: rename_default_persona_owner(
                        new_owner,
                        old_owner,
                        preset_manager=self.preset_manager,
                    ),
                )
            )

            # Provider rename intentionally invalidates in-flight leases and
            # owner-bound idempotency digests. Run it last so a failure in any
            # compensatable store cannot consume those one-way boundaries.
            current_store = "provider"
            result = self.provider_store.rename_owner(old_owner, new_owner)
            stores[current_store] = result
            completed.append(
                (
                    current_store,
                    result,
                    lambda: self.provider_store.rename_owner(new_owner, old_owner),
                )
            )
        except Exception as exc:
            rollback_errors: dict[str, str] = {}
            for label, result, compensate in reversed(completed):
                if not _changed(result):
                    continue
                try:
                    compensate()
                except Exception as rollback_exc:  # pragma: no cover - asserted via metadata
                    rollback_errors[label] = str(rollback_exc)
            receipt = AccountOwnerLifecycleReceipt(
                old_owner,
                new_owner,
                dict(stores),
            )
            raise AccountOwnerLifecycleError(
                f"account owner rename failed in {current_store}",
                receipt=receipt,
                failed_store=current_store,
                rollback_errors=rollback_errors,
            ) from exc

        return AccountOwnerLifecycleReceipt(old_owner, new_owner, dict(stores))

    def purge_owner(self, owner: str) -> AccountOwnerLifecycleReceipt:
        """Idempotently purge one exact owner, reporting any partial closure."""

        owner = _owner(owner)
        stores: dict[str, Any] = {}
        steps = (
            ("provider", lambda: self.provider_store.purge_owner(owner)),
            ("operation_journal", lambda: self.operation_journal.purge_owner(owner)),
            ("artifacts", lambda: self.artifact_store.purge_owner(owner)),
            (
                "default_persona",
                lambda: int(
                    purge_default_persona_owner(
                        owner,
                        preset_manager=self.preset_manager,
                    )
                ),
            ),
        )
        for label, purge in steps:
            try:
                stores[label] = purge()
            except Exception as exc:
                receipt = AccountOwnerLifecycleReceipt(owner, None, dict(stores))
                raise AccountOwnerLifecycleError(
                    f"account owner purge failed in {label}",
                    receipt=receipt,
                    failed_store=label,
                ) from exc
        return AccountOwnerLifecycleReceipt(owner, None, dict(stores))


def build_account_owner_lifecycle(
    preset_manager: Any,
    *,
    session_factory=SessionLocal,
    artifact_root: Path | None = None,
) -> AccountOwnerLifecycle:
    root = Path(artifact_root or (Path(DATA_DIR) / "model-artifacts"))
    return AccountOwnerLifecycle(
        provider_store=ProviderStore(session_factory),
        operation_journal=OperationJournalStore(session_factory),
        artifact_store=ArtifactStore(root, session_factory=session_factory),
        preset_manager=preset_manager,
        operation_path=Path(DATA_DIR) / ".account-lifecycle" / "operations.json",
    )


__all__ = [
    "AccountOwnerLifecycle",
    "AccountOwnerLifecycleError",
    "AccountOwnerLifecycleReceipt",
    "build_account_owner_lifecycle",
]

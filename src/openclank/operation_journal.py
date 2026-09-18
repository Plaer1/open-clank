"""Idempotency and commit-barrier journal for managed model operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Callable, Mapping

from core.database import SessionLocal
from core.operation_models import OperationJournal, operation_utcnow
from core.provider_models import ProviderModelRoute, ProviderOperationBinding


class OperationJournalError(RuntimeError):
    pass


class OperationConflict(OperationJournalError):
    pass


def canonical_request_hash(request: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OperationJournalError("operation request is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


class OperationJournalStore:
    def __init__(
        self,
        session_factory: Callable[..., Any] = SessionLocal,
        *,
        clock: Callable[[], datetime] = operation_utcnow,
    ):
        self._session_factory = session_factory
        self._clock = clock

    @contextmanager
    def _transaction(self):
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _detach(db, row):
        db.flush()
        db.refresh(row)
        db.expunge(row)
        return row

    def begin(
        self,
        *,
        owner: str,
        root_operation_id: str,
        operation: str,
        idempotency_key: str,
        request: Mapping[str, Any],
        connection_id: str,
        billing_lane: str,
        model_route_id: str,
    ) -> tuple[OperationJournal, bool]:
        owner = str(owner or "").strip().lower()
        key = str(idempotency_key or "").strip()
        if not owner or not root_operation_id or len(key) < 16 or len(key) > 128:
            raise OperationJournalError("owner, root operation, and a valid idempotency key are required")
        request_hash = canonical_request_hash(request)
        with self._transaction() as db:
            existing = (
                db.query(OperationJournal)
                .filter(
                    OperationJournal.owner == owner,
                    OperationJournal.idempotency_key == key,
                )
                .first()
            )
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise OperationConflict("idempotency key was used for a different operation")
                return self._detach(db, existing), True
            row = OperationJournal(
                id="op_" + uuid.uuid4().hex,
                owner=owner,
                root_operation_id=str(root_operation_id),
                operation=str(operation),
                idempotency_key=key,
                request_hash=request_hash,
                connection_id=str(connection_id),
                billing_lane=str(billing_lane),
                model_route_id=str(model_route_id),
                state="pending",
            )
            db.add(row)
            return self._detach(db, row), False

    def cas(
        self,
        *,
        owner: str,
        operation_id: str,
        expected_revision: int,
        state: str | None = None,
        binding_id: str | None = None,
        selected_account_id: str | None = None,
        attempt: Mapping[str, Any] | None = None,
        commit_reason: str | None = None,
        artifact_id: str | None = None,
    ) -> OperationJournal:
        owner = str(owner or "").strip().lower()
        with self._transaction() as db:
            row = (
                db.query(OperationJournal)
                .filter(
                    OperationJournal.id == str(operation_id),
                    OperationJournal.owner == owner,
                )
                .first()
            )
            if row is None:
                raise OperationJournalError("operation was not found")
            if row.revision != int(expected_revision):
                raise OperationConflict(
                    f"operation revision is stale; current revision is {row.revision}"
                )
            if row.committed and selected_account_id and selected_account_id != row.selected_account_id:
                raise OperationConflict("a committed operation cannot switch accounts")
            effective_binding_id = str(binding_id or row.binding_id or "").strip()
            if effective_binding_id:
                binding = db.get(ProviderOperationBinding, effective_binding_id)
                route = db.get(ProviderModelRoute, row.model_route_id)
                if (
                    binding is None
                    or route is None
                    or binding.owner != owner
                    or binding.connection_id != row.connection_id
                    or binding.billing_lane != row.billing_lane
                    or route.connection_id != row.connection_id
                    or binding.model_id != route.provider_model_id
                ):
                    raise OperationConflict(
                        "provider binding does not match the journal's selected model"
                    )
                if row.binding_id and row.binding_id != effective_binding_id:
                    raise OperationConflict("an operation cannot switch provider bindings")
                if (
                    selected_account_id
                    and binding.selected_account_id != str(selected_account_id)
                ):
                    raise OperationConflict(
                        "selected account does not match the provider binding"
                    )
            if state:
                row.state = str(state)
            if binding_id:
                row.binding_id = str(binding_id)
            if selected_account_id:
                row.selected_account_id = str(selected_account_id)
            if attempt is not None:
                attempts = list(row.attempts or [])
                attempts.append(dict(attempt))
                row.attempts = attempts
            if commit_reason:
                row.committed = True
                row.commit_reason = str(commit_reason)
            if artifact_id:
                artifacts = list(row.artifact_ids or [])
                if artifact_id not in artifacts:
                    artifacts.append(str(artifact_id))
                row.artifact_ids = artifacts
                row.committed = True
                row.commit_reason = row.commit_reason or "artifact_returned"
            if row.state in {"complete", "failed", "cancelled"}:
                row.completed_at = self._clock()
            row.revision += 1
            return self._detach(db, row)

    def rename_owner(self, old_owner: str, new_owner: str) -> int:
        """Move journal replay authority without merging owner namespaces."""

        old_owner = str(old_owner or "").strip().lower()
        new_owner = str(new_owner or "").strip().lower()
        if not old_owner or not new_owner:
            raise OperationJournalError("old and new operation owners are required")
        if old_owner == new_owner:
            return 0
        with self._transaction() as db:
            if (
                db.query(OperationJournal.id)
                .filter(OperationJournal.owner == new_owner)
                .first()
                is not None
            ):
                raise OperationConflict(
                    "target operation owner already has durable journal state"
                )
            return int(
                db.query(OperationJournal)
                .filter(OperationJournal.owner == old_owner)
                .update(
                    {OperationJournal.owner: new_owner},
                    synchronize_session=False,
                )
            )

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        owner = str(owner or "").strip().lower()
        if not owner:
            raise OperationJournalError("operation owner is required")
        with self._transaction() as db:
            rows = db.query(OperationJournal).filter(
                OperationJournal.owner == owner
            ).order_by(OperationJournal.id).all()
            material = [
                [row.id, int(row.revision), str(row.state), bool(row.committed)]
                for row in rows
            ]
        return {
            "count": len(material),
            "fingerprint": hashlib.sha256(
                json.dumps(material, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
        }

    def purge_owner(self, owner: str) -> int:
        """Delete every operation-journal row for exactly one owner."""

        owner = str(owner or "").strip().lower()
        if not owner:
            raise OperationJournalError("operation owner is required")
        with self._transaction() as db:
            return int(
                db.query(OperationJournal)
                .filter(OperationJournal.owner == owner)
                .delete(synchronize_session=False)
            )


__all__ = [
    "OperationConflict",
    "OperationJournalError",
    "OperationJournalStore",
    "canonical_request_hash",
]

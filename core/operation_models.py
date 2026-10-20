"""Durable operation and artifact records for the managed model router."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import declarative_base


OperationBase = declarative_base()


def operation_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OperationJournal(OperationBase):
    __tablename__ = "model_operation_journal"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    root_operation_id = Column(String, nullable=False, index=True)
    operation = Column(String, nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    connection_id = Column(String, nullable=False)
    billing_lane = Column(String, nullable=False)
    model_route_id = Column(String, nullable=False)
    binding_id = Column(String, nullable=True)
    selected_account_id = Column(String, nullable=True)
    state = Column(String, nullable=False, default="pending", index=True)
    committed = Column(Boolean, nullable=False, default=False)
    commit_reason = Column(String, nullable=True)
    attempts = Column(JSON, nullable=False, default=list)
    artifact_ids = Column(JSON, nullable=False, default=list)
    revision = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, nullable=False, default=operation_utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=operation_utcnow,
        onupdate=operation_utcnow,
    )
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "idempotency_key",
            name="uq_model_operation_owner_idempotency",
        ),
        Index(
            "ix_model_operation_root_connection",
            "owner",
            "root_operation_id",
            "connection_id",
        ),
    )


class ArtifactRecord(OperationBase):
    __tablename__ = "model_artifacts"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    content_sha256 = Column(String(64), nullable=False)
    relative_path = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    media_type = Column(String, nullable=False)
    state = Column(String, nullable=False, default="staged", index=True)
    created_at = Column(DateTime, nullable=False, default=operation_utcnow)
    expires_at = Column(DateTime, nullable=True, index=True)
    acknowledged_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "content_sha256",
            name="uq_model_artifact_owner_content",
        ),
    )


class AccountLifecycleOperation(OperationBase):
    """Authoritative crash-recovery record for one account identity change."""

    __tablename__ = "account_lifecycle_operations"

    id = Column(String, primary_key=True)
    account_id = Column(String, nullable=False, index=True)
    actor_account_id = Column(String, nullable=False)
    kind = Column(String(16), nullable=False, index=True)
    source_owner = Column(String, nullable=False)
    target_owner = Column(String, nullable=True)
    tombstone_owner = Column(String, nullable=True)
    state = Column(String(32), nullable=False, index=True)
    revision = Column(Integer, nullable=False, default=1)
    # One non-terminal operation per immutable account. Clearing this field at
    # completion makes the unique constraint portable across SQLite/Postgres.
    active_account_id = Column(String, nullable=True, unique=True, index=True)
    manifest = Column(JSON, nullable=False, default=dict)
    steps = Column(JSON, nullable=False, default=dict)
    receipt = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=operation_utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=operation_utcnow,
        onupdate=operation_utcnow,
    )
    completed_at = Column(DateTime, nullable=True)


__all__ = [
    "AccountLifecycleOperation",
    "ArtifactRecord",
    "OperationBase",
    "OperationJournal",
    "operation_utcnow",
]

"""Normalized persistence models for Open Clank provider connections.

These tables are intentionally isolated from the legacy ``ModelEndpoint`` and
MiMo auth-store tables.  They form the durable host-side half of the managed
provider protocol: MiMo owns provider semantics while Open Clank owns tenant
scope, encrypted persistence, optimistic revisions, and durable selection.

The provider domain has its own SQLAlchemy metadata so it can be introduced
without coupling its lifecycle to the large legacy model module.  Application
startup creates both metadata collections against the same engine.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, declared_attr


ProviderBase = declarative_base()


def provider_utcnow() -> datetime:
    """Return a naive UTC timestamp, matching the application's DB convention."""

    return datetime.now(timezone.utc).replace(tzinfo=None)


class ProviderTimestampMixin:
    @declared_attr
    def created_at(cls):
        return Column(DateTime, nullable=False, default=provider_utcnow)

    @declared_attr
    def updated_at(cls):
        return Column(
            DateTime,
            nullable=False,
            default=provider_utcnow,
            onupdate=provider_utcnow,
        )


class ProviderConnection(ProviderTimestampMixin, ProviderBase):
    """One owner-scoped route with one immutable billing lane."""

    __tablename__ = "provider_connections"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    family_id = Column(String, nullable=False, index=True)
    adapter_id = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    billing_lane = Column(String, nullable=False, index=True)
    label = Column(String, nullable=False)
    normalized_url = Column(String, nullable=True)
    settings = Column(JSON, nullable=False, default=dict)
    rotation_policy = Column(
        String,
        nullable=False,
        default="equal_round_robin",
    )
    enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_provider_connections_owner_live",
            "owner",
            "enabled",
            "deleted_at",
        ),
    )


class ProviderAccount(ProviderTimestampMixin, ProviderBase):
    """A credential-bearing account inside one connection and billing lane."""

    __tablename__ = "provider_accounts"

    id = Column(String, primary_key=True)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner = Column(String, nullable=False, index=True)
    label = Column(String, nullable=False)
    auth_method = Column(String, nullable=False)
    auth_class = Column(String, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    enabled = Column(Boolean, nullable=False, default=True)
    credential_envelope = Column(Text, nullable=True)
    credential_fingerprint = Column(String(64), nullable=True)
    credential_version = Column(Integer, nullable=False, default=1)
    safe_identity = Column(JSON, nullable=False, default=dict)
    revision = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "credential_fingerprint",
            name="uq_provider_account_connection_credential",
        ),
        Index(
            "ix_provider_accounts_pool",
            "connection_id",
            "enabled",
            "deleted_at",
            "sort_order",
        ),
    )


class ProviderModelRoute(ProviderTimestampMixin, ProviderBase):
    """A stable connection/model identity and its engine-declared operations."""

    __tablename__ = "provider_model_routes"

    id = Column(String, primary_key=True)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    owner = Column(String, nullable=False, index=True)
    provider_model_id = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    operations = Column(JSON, nullable=False, default=list)
    capabilities = Column(JSON, nullable=False, default=dict)
    visibility = Column(String, nullable=False, default="visible")
    catalog_revision = Column(Integer, nullable=False, default=1)
    provenance = Column(JSON, nullable=False, default=dict)
    enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "provider_model_id",
            name="uq_provider_model_route_identity",
        ),
        Index(
            "ix_provider_model_routes_live",
            "connection_id",
            "enabled",
            "deleted_at",
        ),
    )


class ProviderAccountEntitlement(ProviderTimestampMixin, ProviderBase):
    """Whether one account can use one route, independent of route discovery."""

    __tablename__ = "provider_account_entitlements"

    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_route_id = Column(
        String,
        ForeignKey("provider_model_routes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    eligible = Column(Boolean, nullable=False, default=True)
    capability_fingerprint = Column(String, nullable=True)
    evidence = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime, nullable=True)
    revision = Column(Integer, nullable=False, default=1)


class ProviderAccountHealth(ProviderTimestampMixin, ProviderBase):
    """Dynamic whole-account health; never part of credential identity."""

    __tablename__ = "provider_account_health"

    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state = Column(String, nullable=False, default="healthy")
    cooldown_until = Column(DateTime, nullable=True)
    quota_reset_at = Column(DateTime, nullable=True)
    last_error_code = Column(String, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revision = Column(Integer, nullable=False, default=1)


class ProviderAccountModelHealth(ProviderTimestampMixin, ProviderBase):
    """Dynamic health or quarantine scoped to one account/model pair."""

    __tablename__ = "provider_account_model_health"

    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    model_route_id = Column(
        String,
        ForeignKey("provider_model_routes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    state = Column(String, nullable=False, default="healthy")
    cooldown_until = Column(DateTime, nullable=True)
    quota_reset_at = Column(DateTime, nullable=True)
    last_error_code = Column(String, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    revision = Column(Integer, nullable=False, default=1)


class ProviderOperationBinding(ProviderTimestampMixin, ProviderBase):
    """Sticky account selection for a root operation and connection/lane."""

    __tablename__ = "provider_operation_bindings"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False)
    credential_owner = Column(String, nullable=False)
    root_operation_id = Column(String, nullable=False)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_id = Column(String, nullable=False)
    billing_lane = Column(String, nullable=False)
    model_id = Column(String, nullable=False)
    grant_id = Column(String, nullable=True)
    selected_account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="RESTRICT"),
        nullable=True,
    )
    credential_revision = Column(Integer, nullable=True)
    preferred_account_id = Column(String, nullable=True)
    source = Column(String, nullable=False, default="round_robin")
    attempt = Column(Integer, nullable=False, default=1)
    state = Column(String, nullable=False, default="uncommitted")
    committed_at = Column(DateTime, nullable=True)
    fallback_reason = Column(String, nullable=True)
    attempts = Column(JSON, nullable=False, default=list)
    revision = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "root_operation_id",
            "connection_id",
            "billing_lane",
            name="uq_provider_operation_binding_affinity",
        ),
        Index(
            "ix_provider_operation_binding_root",
            "owner",
            "root_operation_id",
        ),
        Index(
            "ix_provider_operation_binding_account",
            "selected_account_id",
            "created_at",
        ),
    )


class ProviderRotationCursor(ProviderTimestampMixin, ProviderBase):
    """Durable equal-round-robin cursor for one owner connection/lane."""

    __tablename__ = "provider_rotation_cursors"

    owner = Column(String, primary_key=True)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    billing_lane = Column(String, primary_key=True)
    next_position = Column(Integer, nullable=False, default=0)
    revision = Column(Integer, nullable=False, default=1)


class ProviderRouteBinding(ProviderTimestampMixin, ProviderBase):
    """One ordered owner default/fallback entry for a model purpose."""

    __tablename__ = "provider_route_bindings"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    purpose = Column(String, nullable=False, index=True)
    ordinal = Column(Integer, nullable=False)
    model_route_id = Column(
        String,
        ForeignKey("provider_model_routes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled = Column(Boolean, nullable=False, default=True)
    revision = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "purpose",
            "ordinal",
            name="uq_provider_route_binding_ordinal",
        ),
        UniqueConstraint(
            "owner",
            "purpose",
            "model_route_id",
            name="uq_provider_route_binding_route",
        ),
        Index(
            "ix_provider_route_bindings_owner_purpose",
            "owner",
            "purpose",
            "enabled",
            "ordinal",
        ),
    )


class ProviderRefreshLease(ProviderTimestampMixin, ProviderBase):
    """Cross-worker single-flight lease for one account credential refresh."""

    __tablename__ = "provider_refresh_leases"

    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    holder_id = Column(String, nullable=False)
    token_digest = Column(String(64), nullable=False)
    credential_revision = Column(Integer, nullable=False)
    acquired_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    max_expires_at = Column(DateTime, nullable=False)
    renewals = Column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "token_digest",
            name="uq_provider_refresh_lease_token",
        ),
    )


class ProviderCredentialLease(ProviderBase):
    """Short-lived credential disclosure bound to an exact model operation."""

    __tablename__ = "provider_credential_leases"

    lease_id_digest = Column(String(64), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    credential_owner = Column(String, nullable=False, index=True)
    holder_id = Column(String, nullable=False, index=True)
    root_operation_id = Column(String, nullable=False)
    binding_id = Column(
        String,
        ForeignKey("provider_operation_bindings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id = Column(
        String,
        ForeignKey("provider_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id = Column(String, nullable=False)
    grant_id = Column(String, nullable=True)
    credential_revision = Column(Integer, nullable=False)
    issued_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


class ProviderShareGrant(ProviderTimestampMixin, ProviderBase):
    """One recipient's account x model selector for a fixed connection/lane."""

    __tablename__ = "provider_share_grants"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    recipient = Column(String, nullable=False, index=True)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    billing_lane = Column(String, nullable=False)
    label = Column(String, nullable=False, default="Shared provider")
    account_selector = Column(JSON, nullable=False)
    model_selector = Column(JSON, nullable=False)
    disclosure_fields = Column(JSON, nullable=False, default=list)
    preferred_account_id = Column(String, nullable=True)
    state = Column(String, nullable=False, default="active")
    revision = Column(Integer, nullable=False, default=1)
    accepted_revision = Column(Integer, nullable=True)

    __table_args__ = (
        Index(
            "ix_provider_share_grants_recipient_live",
            "recipient",
            "state",
            "connection_id",
        ),
    )


class ProviderIdempotencyRecord(ProviderTimestampMixin, ProviderBase):
    """Secret-free replay record for one public provider mutation.

    The caller's idempotency key and request body are represented only by
    installation-keyed digests.  Responses are public API projections and
    therefore cannot contain credential material.
    """

    __tablename__ = "provider_idempotency_records"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    operation = Column(String, nullable=False)
    key_digest = Column(String(64), nullable=False)
    request_digest = Column(String(64), nullable=False)
    status_code = Column(Integer, nullable=False)
    response_body = Column(JSON, nullable=False)
    resource_id = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "operation",
            "key_digest",
            name="uq_provider_idempotency_owner_operation_key",
        ),
    )


class ProviderLegacyAlias(ProviderTimestampMixin, ProviderBase):
    """Nonsecret resolver from retired provider IDs to normalized entities."""

    __tablename__ = "provider_legacy_aliases"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=False, index=True)
    legacy_kind = Column(String, nullable=False)
    legacy_id = Column(String, nullable=False)
    connection_id = Column(
        String,
        ForeignKey("provider_connections.id", ondelete="CASCADE"),
        nullable=True,
    )
    model_route_id = Column(
        String,
        ForeignKey("provider_model_routes.id", ondelete="CASCADE"),
        nullable=True,
    )
    share_grant_id = Column(
        String,
        ForeignKey("provider_share_grants.id", ondelete="CASCADE"),
        nullable=True,
    )
    provenance = Column(JSON, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint(
            "owner",
            "legacy_kind",
            "legacy_id",
            name="uq_provider_legacy_alias_identity",
        ),
    )


PROVIDER_TABLES = (
    ProviderConnection,
    ProviderAccount,
    ProviderModelRoute,
    ProviderAccountEntitlement,
    ProviderAccountHealth,
    ProviderAccountModelHealth,
    ProviderOperationBinding,
    ProviderRotationCursor,
    ProviderRouteBinding,
    ProviderRefreshLease,
    ProviderCredentialLease,
    ProviderShareGrant,
    ProviderIdempotencyRecord,
    ProviderLegacyAlias,
)


__all__ = [
    "ProviderBase",
    "ProviderConnection",
    "ProviderAccount",
    "ProviderModelRoute",
    "ProviderAccountEntitlement",
    "ProviderAccountHealth",
    "ProviderAccountModelHealth",
    "ProviderOperationBinding",
    "ProviderRotationCursor",
    "ProviderRouteBinding",
    "ProviderRefreshLease",
    "ProviderCredentialLease",
    "ProviderShareGrant",
    "ProviderIdempotencyRecord",
    "ProviderLegacyAlias",
    "PROVIDER_TABLES",
    "provider_utcnow",
]

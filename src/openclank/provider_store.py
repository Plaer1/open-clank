"""Owner-scoped repository and selection rules for normalized providers.

The repository is the durable Open Clank boundary used by the managed MiMo
protocol.  It deliberately knows nothing about individual provider token
formats: credentials are opaque JSON objects sealed with authenticated tenant
context, while connection/account semantics are validated by the engine.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
import uuid

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from core.provider_models import (
    ProviderAccount,
    ProviderAccountEntitlement,
    ProviderAccountHealth,
    ProviderAccountModelHealth,
    ProviderConnection,
    ProviderCredentialLease,
    ProviderIdempotencyRecord,
    ProviderLegacyAlias,
    ProviderModelRoute,
    ProviderOperationBinding,
    ProviderRefreshLease,
    ProviderRouteBinding,
    ProviderRotationCursor,
    ProviderShareGrant,
    provider_utcnow,
)
from src.openclank.provider_credentials import (
    CredentialScope,
    credential_fingerprint,
    seal_credential,
    unseal_credential,
)
from src.openclank.provider_family_catalog import bundled_provider_family_catalog
from src.secret_storage import keyed_digest


SAFE_DISCLOSURE_FIELDS = frozenset(
    {
        "account_label",
        "owner_alias",
        "auth_billing_class",
        "provider_display_identity",
        "organization_tenant",
        "enterprise_host",
        "detailed_health",
    }
)

_ACCOUNT_HARD_BLOCK_STATES = frozenset(
    {"disabled", "reauth_required", "revoked"}
)
_MODEL_HARD_BLOCK_STATES = frozenset(
    {"disabled", "ineligible", "revoked"}
)
_KEYLESS_CONNECTION_KINDS = frozenset(
    {"local", "local_server", "local_executor"}
)
_MISSING = object()
_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,63}$")


class ProviderStoreError(RuntimeError):
    """Base class for controlled, secret-free provider-store failures."""


class ProviderValidationError(ProviderStoreError):
    pass


class ProviderNotFound(ProviderStoreError):
    pass


class ProviderConflict(ProviderStoreError):
    pass


class ProviderRevisionConflict(ProviderConflict):
    def __init__(self, *, expected: int, current: int):
        self.expected = int(expected)
        self.current = int(current)
        super().__init__(
            f"provider revision {self.expected} is stale; current revision is {self.current}"
        )


class BillingLaneMismatch(ProviderValidationError):
    pass


class NoEligibleAccount(ProviderStoreError):
    pass


class RefreshLeaseBusy(ProviderConflict):
    pass


class RefreshLeaseInvalid(ProviderConflict):
    pass


class ShareDenied(ProviderStoreError):
    pass


def _required(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise ProviderValidationError(f"{field_name} is required")
    return normalized


def _owner(value: Any) -> str:
    return _required(value, "owner").lower()


def _utc(value: Optional[datetime] = None) -> datetime:
    value = value or provider_utcnow()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _epoch_milliseconds(value: datetime) -> int:
    aware = _utc(value).replace(tzinfo=timezone.utc)
    return int(aware.timestamp() * 1000)


def _json_mapping(value: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ProviderValidationError("value must be an object")
    return dict(value)


def _safe_error_code(value: Any) -> Optional[str]:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    return normalized if _SAFE_ERROR_CODE.fullmatch(normalized) else "provider_error"


def _stable_ids(values: Iterable[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(f"{field_name} must be a list")
    result = sorted({_required(value, field_name) for value in values})
    if not result:
        raise ProviderValidationError(f"{field_name} must not be empty")
    return tuple(result)


def _ordered_ids(values: Iterable[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ProviderValidationError(f"{field_name} must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required(value, field_name)
        if item in seen:
            raise ProviderValidationError(f"{field_name} must not contain duplicates")
        result.append(item)
        seen.add(item)
    if not result:
        raise ProviderValidationError(f"{field_name} must not be empty")
    return tuple(result)


@dataclass(frozen=True)
class AccountSelector:
    """Live pool or explicit stable account IDs."""

    mode: str
    account_ids: tuple[str, ...] = ()

    @classmethod
    def all_live(cls) -> "AccountSelector":
        return cls("all_live_accounts")

    @classmethod
    def explicit(cls, account_ids: Iterable[str]) -> "AccountSelector":
        return cls("explicit_accounts", _stable_ids(account_ids, "account_id"))

    @classmethod
    def parse(cls, value: "AccountSelector | Mapping[str, Any]") -> "AccountSelector":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProviderValidationError("account selector must be an object")
        mode = str(value.get("mode") or "").strip()
        if mode == "all_live_accounts":
            return cls.all_live()
        if mode == "explicit_accounts":
            return cls.explicit(value.get("account_ids") or ())
        raise ProviderValidationError("unsupported account selector mode")

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"mode": self.mode}
        if self.mode == "explicit_accounts":
            payload["account_ids"] = list(self.account_ids)
        return payload

    def includes(self, account_id: str) -> bool:
        return self.mode == "all_live_accounts" or account_id in self.account_ids


@dataclass(frozen=True)
class ModelSelector:
    """Live catalog or explicit stable model-route IDs."""

    mode: str
    model_route_ids: tuple[str, ...] = ()

    @classmethod
    def all_live(cls) -> "ModelSelector":
        return cls("all_live_models")

    @classmethod
    def explicit(cls, model_route_ids: Iterable[str]) -> "ModelSelector":
        return cls(
            "explicit_models",
            _stable_ids(model_route_ids, "model_route_id"),
        )

    @classmethod
    def parse(cls, value: "ModelSelector | Mapping[str, Any]") -> "ModelSelector":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ProviderValidationError("model selector must be an object")
        mode = str(value.get("mode") or "").strip()
        if mode == "all_live_models":
            return cls.all_live()
        if mode == "explicit_models":
            return cls.explicit(value.get("model_route_ids") or ())
        raise ProviderValidationError("unsupported model selector mode")

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"mode": self.mode}
        if self.mode == "explicit_models":
            payload["model_route_ids"] = list(self.model_route_ids)
        return payload

    def includes(self, model_route_id: str) -> bool:
        return self.mode == "all_live_models" or model_route_id in self.model_route_ids


@dataclass(frozen=True)
class CredentialAccess:
    account_id: str
    connection_id: str
    account_revision: int
    credential_version: int
    credentials: Mapping[str, Any] = field(repr=False)


@dataclass(frozen=True)
class AccountSelection:
    binding_id: str
    binding_revision: int
    owner: str
    root_operation_id: str
    connection_id: str
    provider_id: str
    billing_lane: str
    model_id: str
    account_id: Optional[str]
    account_revision: Optional[int]
    credential_version: Optional[int]
    source: str
    attempt: int
    committed: bool
    sticky: bool
    used_preference: bool
    fallback_reason: Optional[str]

    def to_wire(self) -> dict[str, Any]:
        """Managed-provider v1 ``accountBinding`` representation."""

        result = {
            "bindingID": self.binding_id,
            "bindingRevision": self.binding_revision,
            "rootOperationID": self.root_operation_id,
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "modelID": self.model_id,
            "credentialRequired": self.account_id is not None,
            "source": self.source,
            "attempt": self.attempt,
            "committed": self.committed,
        }
        if self.account_id is not None and self.credential_version is not None:
            result["accountID"] = self.account_id
            result["credentialRevision"] = self.credential_version
        return result


@dataclass(frozen=True)
class RefreshLeaseGrant:
    connection_id: str
    account_id: str
    holder_id: str
    credential_revision: int
    expires_at: datetime
    renewable: bool
    token: str = field(repr=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "leaseID": self.token,
            "connectionID": self.connection_id,
            "accountID": self.account_id,
            "credentialRevision": self.credential_revision,
            "expiresAt": _epoch_milliseconds(self.expires_at),
            "renewable": self.renewable,
        }


@dataclass(frozen=True)
class CredentialLeaseGrant:
    lease_id: str
    connection_id: str
    account_id: str
    credential_revision: int
    expires_at: datetime
    credentials: Mapping[str, Any] = field(repr=False)

    def to_wire(self) -> dict[str, Any]:
        return {
            "leaseID": self.lease_id,
            "connectionID": self.connection_id,
            "accountID": self.account_id,
            "credentialRevision": self.credential_revision,
            "expiresAt": _epoch_milliseconds(self.expires_at),
            "credential": dict(self.credentials),
        }


@dataclass(frozen=True)
class ShareScope:
    grant_id: str
    owner: str
    recipient: str
    connection_id: str
    billing_lane: str
    account_ids: tuple[str, ...]
    model_route_ids: tuple[str, ...]
    disclosure_fields: tuple[str, ...]
    preferred_account_id: Optional[str]
    revision: int


@dataclass(frozen=True)
class ReceivedShareProjection:
    """Recipient-safe source rows assembled by one bounded database read.

    Source identifiers remain internal to the route serializer, which turns
    them into recipient/grant-scoped opaque slots.  The projection contains no
    credential envelope or fingerprint.
    """

    grant: ProviderShareGrant
    connection: ProviderConnection
    accounts: tuple[ProviderAccount, ...]
    model_routes: tuple[ProviderModelRoute, ...]
    health_by_account: Mapping[str, tuple[Mapping[str, Any], ...]]


def provider_family_display_name(family_id: str | None) -> str | None:
    """Return the reviewed public name for a provider family.

    Family IDs are safe recipient metadata, but connection/account labels and
    model IDs are not provider identity sources. Unknown or legacy IDs stay
    unresolved so callers can render their explicit localized fallback.
    """

    normalized = str(family_id or "").strip()
    if not normalized:
        return None
    for family in bundled_provider_family_catalog().get("families", ()):
        if isinstance(family, dict) and family.get("id") == normalized:
            display_name = str(family.get("display_name") or "").strip()
            return display_name or None
    return None


@dataclass(frozen=True)
class ProviderManagementSnapshot:
    """One transactionally consistent, nonsecret first-paint snapshot.

    Advanced account, binding, owned-share, and recipient-directory rows stay
    on their lazy endpoints.  Only aggregate account counts cross this seam.
    """

    connections: tuple[ProviderConnection, ...]
    account_counts: Mapping[str, int]
    model_routes: tuple[ProviderModelRoute, ...]
    received_shares: tuple[ReceivedShareProjection, ...]


@dataclass(frozen=True)
class IdempotencyResult:
    status_code: int
    response_body: Mapping[str, Any]
    resource_id: Optional[str]


class ProviderStore:
    """Transactional repository for provider state and account selection."""

    def __init__(
        self,
        session_factory: Callable[..., Any] = SessionLocal,
        *,
        clock: Callable[[], datetime] = provider_utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    @contextmanager
    def _transaction(self):
        db = self._session_factory()
        try:
            yield db
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ProviderConflict("provider state conflicts with an existing record") from exc
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _detach(db: Any, row: Any) -> Any:
        db.flush()
        db.refresh(row)
        db.expunge(row)
        return row

    @staticmethod
    def _detach_many(db: Any, rows: Sequence[Any]) -> list[Any]:
        result = list(rows)
        for row in result:
            db.expunge(row)
        return result

    @staticmethod
    def _require_revision(current: int, expected: int) -> None:
        if int(current) != int(expected):
            raise ProviderRevisionConflict(expected=expected, current=current)

    @staticmethod
    def _claim_row_revision(
        db: Any,
        model: Any,
        filters: Sequence[Any],
        *,
        expected_revision: int,
        missing_message: str,
    ) -> Any:
        """Atomically advance one ORM row's public revision and return it."""

        expected_revision = int(expected_revision)
        query = db.query(model).filter(*filters)
        updated = query.filter(model.revision == expected_revision).update(
            {model.revision: model.revision + 1},
            synchronize_session=False,
        )
        if updated != 1:
            current = query.first()
            if current is None:
                raise ProviderNotFound(missing_message)
            raise ProviderRevisionConflict(
                expected=expected_revision,
                current=int(current.revision),
            )
        db.expire_all()
        row = query.first()
        if row is None:
            raise ProviderNotFound(missing_message)
        return row

    @staticmethod
    def _require_connection(
        db: Any,
        owner: str,
        connection_id: str,
        *,
        require_live: bool = True,
    ) -> ProviderConnection:
        row = (
            db.query(ProviderConnection)
            .filter(
                ProviderConnection.id == connection_id,
                ProviderConnection.owner == owner,
            )
            .first()
        )
        if row is None or (require_live and row.deleted_at is not None):
            raise ProviderNotFound("provider connection was not found")
        return row

    @staticmethod
    def _require_account(
        db: Any,
        owner: str,
        account_id: str,
        *,
        require_live: bool = True,
    ) -> ProviderAccount:
        row = (
            db.query(ProviderAccount)
            .filter(
                ProviderAccount.id == account_id,
                ProviderAccount.owner == owner,
            )
            .first()
        )
        if row is None or (require_live and row.deleted_at is not None):
            raise ProviderNotFound("provider account was not found")
        return row

    @staticmethod
    def _require_route(
        db: Any,
        owner: str,
        model_route_id: str,
        *,
        require_live: bool = True,
    ) -> ProviderModelRoute:
        row = (
            db.query(ProviderModelRoute)
            .filter(
                ProviderModelRoute.id == model_route_id,
                ProviderModelRoute.owner == owner,
            )
            .first()
        )
        if row is None or (require_live and row.deleted_at is not None):
            raise ProviderNotFound("provider model route was not found")
        return row

    # ------------------------------------------------------------------
    # Connection, account, and route lifecycle
    # ------------------------------------------------------------------

    def create_connection(
        self,
        *,
        owner: str,
        family_id: str,
        adapter_id: str,
        kind: str,
        billing_lane: str,
        label: str,
        normalized_url: Optional[str] = None,
        settings: Optional[Mapping[str, Any]] = None,
        connection_id: Optional[str] = None,
        enabled: bool = True,
    ) -> ProviderConnection:
        owner = _owner(owner)
        row = ProviderConnection(
            id=connection_id or f"pcn_{uuid.uuid4().hex}",
            owner=owner,
            family_id=_required(family_id, "family_id"),
            adapter_id=_required(adapter_id, "adapter_id"),
            kind=_required(kind, "kind"),
            billing_lane=_required(billing_lane, "billing_lane"),
            label=_required(label, "label"),
            normalized_url=(str(normalized_url).strip() if normalized_url else None),
            settings=_json_mapping(settings),
            enabled=bool(enabled),
        )
        with self._transaction() as db:
            db.add(row)
            return self._detach(db, row)

    def create_connection_with_routes(
        self,
        *,
        owner: str,
        family_id: str,
        adapter_id: str,
        kind: str,
        billing_lane: str,
        label: str,
        model_routes: Iterable[Mapping[str, Any]],
        normalized_url: Optional[str] = None,
        settings: Optional[Mapping[str, Any]] = None,
        connection_id: Optional[str] = None,
        enabled: bool = True,
    ) -> tuple[ProviderConnection, list[ProviderModelRoute]]:
        """Atomically persist a connection and engine-declared model routes."""

        owner = _owner(owner)
        connection = ProviderConnection(
            id=connection_id or f"pcn_{uuid.uuid4().hex}",
            owner=owner,
            family_id=_required(family_id, "family_id"),
            adapter_id=_required(adapter_id, "adapter_id"),
            kind=_required(kind, "kind"),
            billing_lane=_required(billing_lane, "billing_lane"),
            label=_required(label, "label"),
            normalized_url=(str(normalized_url).strip() if normalized_url else None),
            settings=_json_mapping(settings),
            enabled=bool(enabled),
        )
        routes: list[ProviderModelRoute] = []
        seen_models: set[str] = set()
        for raw in model_routes:
            value = dict(raw)
            model_id = _required(value.get("provider_model_id"), "provider_model_id")
            if model_id in seen_models:
                raise ProviderValidationError("engine model routes contain a duplicate model")
            seen_models.add(model_id)
            operations = sorted(
                {_required(item, "operation") for item in value.get("operations") or ()}
            )
            if not operations:
                raise ProviderValidationError("engine model route has no operations")
            routes.append(
                ProviderModelRoute(
                    id=_required(value.get("id"), "model_route_id"),
                    connection_id=connection.id,
                    owner=owner,
                    provider_model_id=model_id,
                    display_name=_required(
                        value.get("display_name") or model_id,
                        "display_name",
                    ),
                    operations=operations,
                    capabilities=_json_mapping(value.get("capabilities")),
                    provenance=_json_mapping(value.get("provenance")),
                    enabled=bool(value.get("enabled", True)),
                )
            )
        with self._transaction() as db:
            db.add(connection)
            db.add_all(routes)
            db.flush()
            return self._detach(db, connection), self._detach_many(db, routes)

    def get_connection(self, *, owner: str, connection_id: str) -> ProviderConnection:
        owner = _owner(owner)
        with self._transaction() as db:
            return self._detach(
                db,
                self._require_connection(db, owner, _required(connection_id, "connection_id")),
            )

    def list_connections(self, *, owner: str, include_deleted: bool = False) -> list[ProviderConnection]:
        owner = _owner(owner)
        with self._transaction() as db:
            query = db.query(ProviderConnection).filter(ProviderConnection.owner == owner)
            if not include_deleted:
                query = query.filter(ProviderConnection.deleted_at.is_(None))
            rows = query.order_by(ProviderConnection.created_at, ProviderConnection.id).all()
            return self._detach_many(db, rows)

    def update_connection(
        self,
        *,
        owner: str,
        connection_id: str,
        expected_revision: int,
        label: Any = _MISSING,
        normalized_url: Any = _MISSING,
        settings: Any = _MISSING,
        enabled: Any = _MISSING,
    ) -> ProviderConnection:
        """Update mutable connection metadata without changing its auth lane."""

        owner = _owner(owner)
        with self._transaction() as db:
            connection_id = _required(connection_id, "connection_id")
            row = self._claim_row_revision(
                db,
                ProviderConnection,
                (
                    ProviderConnection.id == connection_id,
                    ProviderConnection.owner == owner,
                    ProviderConnection.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider connection was not found",
            )
            if label is not _MISSING:
                row.label = _required(label, "label")
            if normalized_url is not _MISSING:
                value = str(normalized_url or "").strip()
                row.normalized_url = value or None
            if settings is not _MISSING:
                row.settings = _json_mapping(settings)
            if enabled is not _MISSING:
                row.enabled = bool(enabled)
            return self._detach(db, row)

    def delete_connection(
        self,
        *,
        owner: str,
        connection_id: str,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> ProviderConnection:
        """Create credential-free tombstones for a connection and its accounts."""

        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            connection_id = _required(connection_id, "connection_id")
            row = self._claim_row_revision(
                db,
                ProviderConnection,
                (
                    ProviderConnection.id == connection_id,
                    ProviderConnection.owner == owner,
                    ProviderConnection.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider connection was not found",
            )
            accounts = db.query(ProviderAccount).filter(
                ProviderAccount.owner == owner,
                ProviderAccount.connection_id == row.id,
                ProviderAccount.deleted_at.is_(None),
            ).all()
            account_ids = [account.id for account in accounts]
            for account in accounts:
                account.enabled = False
                account.credential_envelope = None
                account.credential_fingerprint = None
                account.credential_version += 1
                account.safe_identity = {}
                account.deleted_at = timestamp
                account.revision += 1
            if account_ids:
                db.query(ProviderRefreshLease).filter(
                    ProviderRefreshLease.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                db.query(ProviderCredentialLease).filter(
                    ProviderCredentialLease.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
            routes = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.connection_id == row.id,
                ProviderModelRoute.deleted_at.is_(None),
            ).all()
            route_ids = [route.id for route in routes]
            for route in routes:
                route.enabled = False
                route.deleted_at = timestamp
                route.revision += 1
            if route_ids:
                bindings = db.query(ProviderRouteBinding).filter(
                    ProviderRouteBinding.owner == owner,
                    ProviderRouteBinding.model_route_id.in_(route_ids),
                    ProviderRouteBinding.enabled.is_(True),
                ).all()
                for binding in bindings:
                    binding.enabled = False
                    binding.revision += 1
            grants = db.query(ProviderShareGrant).filter(
                ProviderShareGrant.owner == owner,
                ProviderShareGrant.connection_id == row.id,
                ProviderShareGrant.state == "active",
            ).all()
            for grant in grants:
                grant.state = "revoked"
                grant.revision += 1
            row.enabled = False
            row.deleted_at = timestamp
            return self._detach(db, row)

    def create_account(
        self,
        *,
        owner: str,
        connection_id: str,
        label: str,
        auth_method: str,
        auth_class: str,
        credentials: Mapping[str, Any],
        account_id: Optional[str] = None,
        sort_order: Optional[int] = None,
        safe_identity: Optional[Mapping[str, Any]] = None,
        enabled: bool = True,
    ) -> ProviderAccount:
        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        account_id = account_id or f"pac_{uuid.uuid4().hex}"
        account_id = _required(account_id, "account_id")
        fingerprint = credential_fingerprint(
            credentials,
            owner=owner,
            connection_id=connection_id,
        )
        scope = CredentialScope(owner, connection_id, account_id, 1)
        envelope = seal_credential(credentials, scope)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, connection_id)
            duplicate = (
                db.query(ProviderAccount.id)
                .filter(
                    ProviderAccount.connection_id == connection_id,
                    ProviderAccount.credential_fingerprint == fingerprint,
                    ProviderAccount.deleted_at.is_(None),
                )
                .first()
            )
            if duplicate is not None:
                raise ProviderConflict(
                    "credential is already registered for this connection"
                )
            if sort_order is None:
                last_account = (
                    db.query(ProviderAccount.sort_order)
                    .filter(
                        ProviderAccount.owner == owner,
                        ProviderAccount.connection_id == connection_id,
                        ProviderAccount.deleted_at.is_(None),
                    )
                    .order_by(
                        ProviderAccount.sort_order.desc(),
                        ProviderAccount.id.desc(),
                    )
                    .first()
                )
                resolved_sort_order = (
                    int(last_account[0]) + 1 if last_account is not None else 0
                )
            else:
                resolved_sort_order = int(sort_order)
            row = ProviderAccount(
                id=account_id,
                connection_id=connection_id,
                owner=owner,
                label=_required(label, "label"),
                auth_method=_required(auth_method, "auth_method"),
                auth_class=_required(auth_class, "auth_class"),
                sort_order=resolved_sort_order,
                enabled=bool(enabled),
                credential_envelope=envelope,
                credential_fingerprint=fingerprint,
                credential_version=1,
                safe_identity=_json_mapping(safe_identity),
            )
            db.add(row)
            db.query(ProviderConnection).filter(
                ProviderConnection.id == connection.id,
                ProviderConnection.owner == owner,
            ).update(
                {ProviderConnection.revision: ProviderConnection.revision + 1},
                synchronize_session=False,
            )
            return self._detach(db, row)

    def get_account(self, *, owner: str, account_id: str) -> ProviderAccount:
        owner = _owner(owner)
        with self._transaction() as db:
            return self._detach(
                db,
                self._require_account(db, owner, _required(account_id, "account_id")),
            )

    def list_accounts(
        self,
        *,
        owner: str,
        connection_id: str,
        include_deleted: bool = False,
    ) -> list[ProviderAccount]:
        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        with self._transaction() as db:
            self._require_connection(db, owner, connection_id, require_live=not include_deleted)
            query = db.query(ProviderAccount).filter(
                ProviderAccount.owner == owner,
                ProviderAccount.connection_id == connection_id,
            )
            if not include_deleted:
                query = query.filter(ProviderAccount.deleted_at.is_(None))
            rows = query.order_by(ProviderAccount.sort_order, ProviderAccount.id).all()
            return self._detach_many(db, rows)

    def update_account(
        self,
        *,
        owner: str,
        account_id: str,
        expected_revision: int,
        label: Any = _MISSING,
        enabled: Any = _MISSING,
        sort_order: Any = _MISSING,
        safe_identity: Any = _MISSING,
        credentials: Any = _MISSING,
    ) -> ProviderAccount:
        owner = _owner(owner)
        with self._transaction() as db:
            account_id = _required(account_id, "account_id")
            row = self._claim_row_revision(
                db,
                ProviderAccount,
                (
                    ProviderAccount.id == account_id,
                    ProviderAccount.owner == owner,
                    ProviderAccount.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider account was not found",
            )
            pool_changed = False
            if label is not _MISSING:
                row.label = _required(label, "label")
            if enabled is not _MISSING:
                pool_changed = bool(row.enabled) != bool(enabled)
                row.enabled = bool(enabled)
            if sort_order is not _MISSING:
                pool_changed = pool_changed or int(row.sort_order) != int(sort_order)
                row.sort_order = int(sort_order)
            if safe_identity is not _MISSING:
                row.safe_identity = _json_mapping(safe_identity)
            if credentials is not _MISSING:
                row = self._install_credential(
                    db,
                    row,
                    credentials,
                    expected_credential_version=row.credential_version,
                    bump_revision=False,
                )
            if pool_changed:
                db.query(ProviderConnection).filter(
                    ProviderConnection.id == row.connection_id,
                    ProviderConnection.owner == owner,
                    ProviderConnection.deleted_at.is_(None),
                ).update(
                    {ProviderConnection.revision: ProviderConnection.revision + 1},
                    synchronize_session=False,
                )
            return self._detach(db, row)

    def delete_account(
        self,
        *,
        owner: str,
        account_id: str,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> ProviderAccount:
        """Credential-free tombstone preserving stable IDs and grant history."""

        owner = _owner(owner)
        with self._transaction() as db:
            account_id = _required(account_id, "account_id")
            row = self._claim_row_revision(
                db,
                ProviderAccount,
                (
                    ProviderAccount.id == account_id,
                    ProviderAccount.owner == owner,
                    ProviderAccount.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider account was not found",
            )
            connection = self._require_connection(db, owner, row.connection_id)
            row.enabled = False
            row.credential_envelope = None
            row.credential_fingerprint = None
            row.credential_version += 1
            row.safe_identity = {}
            row.deleted_at = _utc(now or self._clock())
            db.query(ProviderConnection).filter(
                ProviderConnection.id == connection.id,
                ProviderConnection.owner == owner,
            ).update(
                {ProviderConnection.revision: ProviderConnection.revision + 1},
                synchronize_session=False,
            )
            db.query(ProviderRefreshLease).filter(
                ProviderRefreshLease.account_id == row.id
            ).delete(synchronize_session=False)
            db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id == row.id
            ).delete(synchronize_session=False)
            db.query(ProviderAccountModelHealth).filter(
                ProviderAccountModelHealth.account_id == row.id
            ).delete(synchronize_session=False)
            db.query(ProviderCredentialLease).filter(
                ProviderCredentialLease.account_id == row.id
            ).delete(synchronize_session=False)
            return self._detach(db, row)

    def credential_access(self, *, owner: str, account_id: str) -> CredentialAccess:
        owner = _owner(owner)
        with self._transaction() as db:
            row = self._require_account(db, owner, _required(account_id, "account_id"))
            if not row.credential_envelope:
                raise ProviderNotFound("provider account has no credential")
            payload = unseal_credential(
                row.credential_envelope,
                CredentialScope(
                    row.owner,
                    row.connection_id,
                    row.id,
                    row.credential_version,
                ),
            )
            return CredentialAccess(
                account_id=row.id,
                connection_id=row.connection_id,
                account_revision=row.revision,
                credential_version=row.credential_version,
                credentials=payload,
            )

    def replace_account_credential(
        self,
        *,
        owner: str,
        account_id: str,
        expected_credential_version: int,
        credentials: Mapping[str, Any],
    ) -> ProviderAccount:
        owner = _owner(owner)
        with self._transaction() as db:
            row = self._require_account(db, owner, _required(account_id, "account_id"))
            row = self._install_credential(
                db,
                row,
                credentials,
                expected_credential_version=expected_credential_version,
            )
            return self._detach(db, row)

    @staticmethod
    def _install_credential(
        db: Any,
        account: ProviderAccount,
        credentials: Mapping[str, Any],
        *,
        expected_credential_version: Optional[int] = None,
        bump_revision: bool = True,
    ) -> ProviderAccount:
        fingerprint = credential_fingerprint(
            credentials,
            owner=account.owner,
            connection_id=account.connection_id,
        )
        duplicate = (
            db.query(ProviderAccount.id)
            .filter(
                ProviderAccount.connection_id == account.connection_id,
                ProviderAccount.credential_fingerprint == fingerprint,
                ProviderAccount.id != account.id,
                ProviderAccount.deleted_at.is_(None),
            )
            .first()
        )
        if duplicate is not None:
            raise ProviderConflict(
                "credential is already registered for this connection"
            )
        expected_version = int(
            account.credential_version
            if expected_credential_version is None
            else expected_credential_version
        )
        next_version = expected_version + 1
        envelope = seal_credential(
            credentials,
            CredentialScope(
                account.owner,
                account.connection_id,
                account.id,
                next_version,
            ),
        )
        values: dict[Any, Any] = {
            ProviderAccount.credential_envelope: envelope,
            ProviderAccount.credential_fingerprint: fingerprint,
            ProviderAccount.credential_version: next_version,
        }
        if bump_revision:
            values[ProviderAccount.revision] = ProviderAccount.revision + 1
        updated = db.query(ProviderAccount).filter(
            ProviderAccount.id == account.id,
            ProviderAccount.owner == account.owner,
            ProviderAccount.deleted_at.is_(None),
            ProviderAccount.credential_version == expected_version,
        ).update(values, synchronize_session=False)
        if updated != 1:
            current = db.query(ProviderAccount).filter(
                ProviderAccount.id == account.id,
                ProviderAccount.owner == account.owner,
            ).first()
            if current is None or current.deleted_at is not None:
                raise ProviderNotFound("provider account was not found")
            raise ProviderRevisionConflict(
                expected=expected_version,
                current=int(current.credential_version),
            )
        db.expire_all()
        current = db.query(ProviderAccount).filter(
            ProviderAccount.id == account.id,
            ProviderAccount.owner == account.owner,
            ProviderAccount.deleted_at.is_(None),
        ).first()
        if current is None:
            raise ProviderNotFound("provider account was not found")
        return current

    def create_model_route(
        self,
        *,
        owner: str,
        connection_id: str,
        provider_model_id: str,
        display_name: Optional[str] = None,
        operations: Iterable[str] = ("chat.stream", "chat.complete"),
        capabilities: Optional[Mapping[str, Any]] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        model_route_id: Optional[str] = None,
        enabled: bool = True,
    ) -> ProviderModelRoute:
        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        provider_model_id = _required(provider_model_id, "provider_model_id")
        operation_list = sorted(
            {_required(value, "operation") for value in operations}
        )
        if not operation_list:
            raise ProviderValidationError("at least one operation is required")
        with self._transaction() as db:
            self._require_connection(db, owner, connection_id)
            row = ProviderModelRoute(
                id=model_route_id or f"pmr_{uuid.uuid4().hex}",
                connection_id=connection_id,
                owner=owner,
                provider_model_id=provider_model_id,
                display_name=_required(display_name or provider_model_id, "display_name"),
                operations=operation_list,
                capabilities=_json_mapping(capabilities),
                provenance=_json_mapping(provenance),
                enabled=bool(enabled),
            )
            db.add(row)
            return self._detach(db, row)

    def get_model_route(
        self,
        *,
        owner: str,
        model_route_id: str,
    ) -> ProviderModelRoute:
        owner = _owner(owner)
        with self._transaction() as db:
            return self._detach(
                db,
                self._require_route(
                    db,
                    owner,
                    _required(model_route_id, "model_route_id"),
                ),
            )

    def list_model_routes(
        self,
        *,
        owner: str,
        connection_id: Optional[str] = None,
        include_deleted: bool = False,
    ) -> list[ProviderModelRoute]:
        owner = _owner(owner)
        connection_id = (
            _required(connection_id, "connection_id")
            if connection_id is not None
            else None
        )
        with self._transaction() as db:
            if connection_id is not None:
                self._require_connection(
                    db,
                    owner,
                    connection_id,
                    require_live=not include_deleted,
                )
            query = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
            )
            if connection_id is not None:
                query = query.filter(
                    ProviderModelRoute.connection_id == connection_id,
                )
            if not include_deleted:
                query = query.filter(ProviderModelRoute.deleted_at.is_(None))
            rows = query.order_by(
                ProviderModelRoute.connection_id,
                ProviderModelRoute.display_name,
                ProviderModelRoute.id,
            ).all()
            return self._detach_many(db, rows)

    def sync_model_routes(
        self,
        *,
        owner: str,
        connection_id: str,
        model_routes: Iterable[Mapping[str, Any]],
        expected_revision: Optional[int] = None,
        eligible_account_id: Optional[str] = None,
        entitle_all_accounts: bool = False,
        now: Optional[datetime] = None,
    ) -> tuple[ProviderConnection, list[ProviderModelRoute]]:
        """Replace one connection's engine-owned catalog without changing route IDs."""

        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        normalized: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for raw in model_routes:
            value = dict(raw)
            model_id = _required(value.get("provider_model_id"), "provider_model_id")
            if model_id in seen_models:
                raise ProviderValidationError("engine model routes contain a duplicate model")
            seen_models.add(model_id)
            operations = sorted(
                {_required(item, "operation") for item in value.get("operations") or ()}
            )
            if not operations:
                raise ProviderValidationError("engine model route has no operations")
            normalized.append(
                {
                    "id": _required(value.get("id"), "model_route_id"),
                    "provider_model_id": model_id,
                    "display_name": _required(
                        value.get("display_name") or model_id,
                        "display_name",
                    ),
                    "operations": operations,
                    "capabilities": _json_mapping(value.get("capabilities")),
                    "provenance": _json_mapping(value.get("provenance")),
                    "enabled": bool(value.get("enabled", True)),
                }
            )

        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            filters = (
                ProviderConnection.id == connection_id,
                ProviderConnection.owner == owner,
                ProviderConnection.deleted_at.is_(None),
            )
            if expected_revision is None:
                connection = self._require_connection(db, owner, connection_id)
                connection.revision += 1
            else:
                connection = self._claim_row_revision(
                    db,
                    ProviderConnection,
                    filters,
                    expected_revision=expected_revision,
                    missing_message="provider connection was not found",
                )
            if eligible_account_id is not None and entitle_all_accounts:
                raise ProviderValidationError(
                    "model sync account entitlement modes are mutually exclusive"
                )
            eligible_accounts: list[ProviderAccount] = []
            if eligible_account_id is not None:
                account = self._require_account(
                    db,
                    owner,
                    _required(eligible_account_id, "account_id"),
                )
                if account.connection_id != connection.id:
                    raise ProviderValidationError(
                        "account and model routes belong to different connections"
                    )
                eligible_accounts.append(account)
            elif entitle_all_accounts:
                eligible_accounts = db.query(ProviderAccount).filter(
                    ProviderAccount.owner == owner,
                    ProviderAccount.connection_id == connection.id,
                    ProviderAccount.deleted_at.is_(None),
                    ProviderAccount.enabled.is_(True),
                ).all()

            current_rows = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.connection_id == connection.id,
            ).all()
            current = {row.provider_model_id: row for row in current_rows}
            catalog_revision = max(
                (int(row.catalog_revision or 0) for row in current_rows),
                default=0,
            ) + 1
            active_rows: list[ProviderModelRoute] = []
            for value in normalized:
                row = current.get(value["provider_model_id"])
                if row is None:
                    row = ProviderModelRoute(
                        id=value["id"],
                        connection_id=connection.id,
                        owner=owner,
                        provider_model_id=value["provider_model_id"],
                    )
                    db.add(row)
                else:
                    row.revision += 1
                row.display_name = value["display_name"]
                row.operations = value["operations"]
                row.capabilities = value["capabilities"]
                row.provenance = value["provenance"]
                row.catalog_revision = catalog_revision
                row.enabled = value["enabled"]
                row.deleted_at = None
                active_rows.append(row)

            removed_ids: list[str] = []
            for model_id, row in current.items():
                if model_id in seen_models or row.deleted_at is not None:
                    continue
                row.enabled = False
                row.deleted_at = timestamp
                row.catalog_revision = catalog_revision
                row.revision += 1
                removed_ids.append(row.id)
            if removed_ids:
                bindings = db.query(ProviderRouteBinding).filter(
                    ProviderRouteBinding.owner == owner,
                    ProviderRouteBinding.model_route_id.in_(removed_ids),
                    ProviderRouteBinding.enabled.is_(True),
                ).all()
                for binding in bindings:
                    binding.enabled = False
                    binding.revision += 1

            if eligible_accounts:
                # Flush new route identities before inserting entitlement FKs;
                # these mappers intentionally have no ORM relationships.
                db.flush()
                active_ids = {row.id for row in active_rows}
                for account in eligible_accounts:
                    existing_entitlements = {
                        item.model_route_id: item
                        for item in db.query(ProviderAccountEntitlement).filter(
                            ProviderAccountEntitlement.account_id == account.id,
                        ).all()
                    }
                    for row in active_rows:
                        entitlement = existing_entitlements.get(row.id)
                        if entitlement is None:
                            entitlement = ProviderAccountEntitlement(
                                account_id=account.id,
                                model_route_id=row.id,
                            )
                            db.add(entitlement)
                        else:
                            entitlement.revision += 1
                        entitlement.eligible = bool(row.enabled)
                        entitlement.evidence = {"authority": "managed-engine"}
                        entitlement.last_seen_at = timestamp
                    for route_id, entitlement in existing_entitlements.items():
                        if route_id in active_ids:
                            continue
                        entitlement.eligible = False
                        entitlement.revision += 1
                        entitlement.last_seen_at = timestamp

            detached_connection = self._detach(db, connection)
            return detached_connection, self._detach_many(db, active_rows)

    def entitle_account_routes(
        self,
        *,
        owner: str,
        account_id: str,
        now: Optional[datetime] = None,
    ) -> int:
        """Make an account eligible for the connection's current live catalog."""

        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(
                db,
                owner,
                _required(account_id, "account_id"),
            )
            routes = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.connection_id == account.connection_id,
                ProviderModelRoute.deleted_at.is_(None),
                ProviderModelRoute.enabled.is_(True),
            ).all()
            existing = {
                row.model_route_id: row
                for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id == account.id,
                ).all()
            }
            for route in routes:
                row = existing.get(route.id)
                if row is None:
                    row = ProviderAccountEntitlement(
                        account_id=account.id,
                        model_route_id=route.id,
                    )
                    db.add(row)
                else:
                    row.revision += 1
                row.eligible = True
                row.evidence = {"authority": "managed-engine"}
                row.last_seen_at = timestamp
            return len(routes)

    def model_eligibility(
        self,
        *,
        owner: str,
        model_route_id: str,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Return a safe per-account eligibility projection for route owners."""

        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            route = self._require_route(
                db,
                owner,
                _required(model_route_id, "model_route_id"),
            )
            accounts = db.query(ProviderAccount).filter(
                ProviderAccount.owner == owner,
                ProviderAccount.connection_id == route.connection_id,
                ProviderAccount.deleted_at.is_(None),
            ).order_by(ProviderAccount.sort_order, ProviderAccount.id).all()
            account_ids = [account.id for account in accounts]
            if not account_ids:
                return []
            entitlements = {
                row.account_id: row
                for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                    ProviderAccountEntitlement.model_route_id == route.id,
                ).all()
            }
            account_health = {
                row.account_id: row
                for row in db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_(account_ids),
                ).all()
            }
            model_health = {
                row.account_id: row
                for row in db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids),
                    ProviderAccountModelHealth.model_route_id == route.id,
                ).all()
            }
            result: list[dict[str, Any]] = []
            for account in accounts:
                entitlement = entitlements.get(account.id)
                whole = account_health.get(account.id)
                scoped = model_health.get(account.id)
                entitled = bool(entitlement and entitlement.eligible)
                available = (
                    bool(account.enabled)
                    and bool(route.enabled)
                    and entitled
                    and (
                        whole is None
                        or self._health_available(
                            whole.state,
                            whole.cooldown_until,
                            quota_reset_at=whole.quota_reset_at,
                            now=timestamp,
                        )
                    )
                    and (
                        scoped is None
                        or self._health_available(
                            scoped.state,
                            scoped.cooldown_until,
                            quota_reset_at=scoped.quota_reset_at,
                            now=timestamp,
                            model_scope=True,
                        )
                    )
                )
                result.append(
                    {
                        "account_id": account.id,
                        "entitled": entitled,
                        "eligible": available,
                        "account_state": str(
                            getattr(whole, "state", None) or "healthy"
                        ),
                        "model_state": str(
                            getattr(scoped, "state", None) or "healthy"
                        ),
                        "account_cooldown_until": getattr(
                            whole, "cooldown_until", None
                        ),
                        "account_quota_reset_at": getattr(
                            whole, "quota_reset_at", None
                        ),
                        "account_last_error_code": getattr(
                            whole, "last_error_code", None
                        ),
                        "model_cooldown_until": getattr(
                            scoped, "cooldown_until", None
                        ),
                        "model_quota_reset_at": getattr(
                            scoped, "quota_reset_at", None
                        ),
                        "model_last_error_code": getattr(
                            scoped, "last_error_code", None
                        ),
                    }
                )
            return result

    def connection_eligibility(
        self,
        *,
        owner: str,
        connection_id: str,
        now: Optional[datetime] = None,
    ) -> list[dict[str, Any]]:
        """Return every route/account health projection with fixed query count.

        The settings surface expands one provider at a time.  Reading each
        model through :meth:`model_eligibility` made that expansion issue one
        HTTP request and several SQL queries per route.  This projection reads
        the connection, routes, accounts, entitlements, and two health tables
        once each, independent of route/account cardinality.
        """

        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            self._require_connection(db, owner, connection_id)
            routes = (
                db.query(ProviderModelRoute)
                .filter(
                    ProviderModelRoute.owner == owner,
                    ProviderModelRoute.connection_id == connection_id,
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .order_by(
                    ProviderModelRoute.provider_model_id,
                    ProviderModelRoute.id,
                )
                .all()
            )
            accounts = (
                db.query(ProviderAccount)
                .filter(
                    ProviderAccount.owner == owner,
                    ProviderAccount.connection_id == connection_id,
                    ProviderAccount.deleted_at.is_(None),
                )
                .order_by(ProviderAccount.sort_order, ProviderAccount.id)
                .all()
            )
            route_ids = [route.id for route in routes]
            account_ids = [account.id for account in accounts]
            if not route_ids or not account_ids:
                return [
                    {"model_route_id": route.id, "accounts": []}
                    for route in routes
                ]

            entitlements = {
                (row.model_route_id, row.account_id): row
                for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                    ProviderAccountEntitlement.model_route_id.in_(route_ids),
                ).all()
            }
            account_health = {
                row.account_id: row
                for row in db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_(account_ids),
                ).all()
            }
            model_health = {
                (row.model_route_id, row.account_id): row
                for row in db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids),
                    ProviderAccountModelHealth.model_route_id.in_(route_ids),
                ).all()
            }

            result: list[dict[str, Any]] = []
            for route in routes:
                rows: list[dict[str, Any]] = []
                for account in accounts:
                    entitlement = entitlements.get((route.id, account.id))
                    whole = account_health.get(account.id)
                    scoped = model_health.get((route.id, account.id))
                    entitled = bool(entitlement and entitlement.eligible)
                    available = (
                        bool(account.enabled)
                        and bool(route.enabled)
                        and entitled
                        and (
                            whole is None
                            or self._health_available(
                                whole.state,
                                whole.cooldown_until,
                                quota_reset_at=whole.quota_reset_at,
                                now=timestamp,
                            )
                        )
                        and (
                            scoped is None
                            or self._health_available(
                                scoped.state,
                                scoped.cooldown_until,
                                quota_reset_at=scoped.quota_reset_at,
                                now=timestamp,
                                model_scope=True,
                            )
                        )
                    )
                    rows.append(
                        {
                            "account_id": account.id,
                            "entitled": entitled,
                            "eligible": available,
                            "account_state": str(
                                getattr(whole, "state", None) or "healthy"
                            ),
                            "model_state": str(
                                getattr(scoped, "state", None) or "healthy"
                            ),
                            "account_cooldown_until": getattr(
                                whole, "cooldown_until", None
                            ),
                            "account_quota_reset_at": getattr(
                                whole, "quota_reset_at", None
                            ),
                            "account_last_error_code": getattr(
                                whole, "last_error_code", None
                            ),
                            "model_cooldown_until": getattr(
                                scoped, "cooldown_until", None
                            ),
                            "model_quota_reset_at": getattr(
                                scoped, "quota_reset_at", None
                            ),
                            "model_last_error_code": getattr(
                                scoped, "last_error_code", None
                            ),
                        }
                    )
                result.append({"model_route_id": route.id, "accounts": rows})
            return result

    def set_entitlement(
        self,
        *,
        owner: str,
        account_id: str,
        model_route_id: str,
        eligible: bool,
        capability_fingerprint: Optional[str] = None,
        evidence: Optional[Mapping[str, Any]] = None,
        last_seen_at: Optional[datetime] = None,
    ) -> ProviderAccountEntitlement:
        owner = _owner(owner)
        with self._transaction() as db:
            account = self._require_account(db, owner, _required(account_id, "account_id"))
            route = self._require_route(
                db,
                owner,
                _required(model_route_id, "model_route_id"),
            )
            if account.connection_id != route.connection_id:
                raise ProviderValidationError(
                    "account and model route belong to different connections"
                )
            row = (
                db.query(ProviderAccountEntitlement)
                .filter(
                    ProviderAccountEntitlement.account_id == account.id,
                    ProviderAccountEntitlement.model_route_id == route.id,
                )
                .first()
            )
            if row is None:
                row = ProviderAccountEntitlement(
                    account_id=account.id,
                    model_route_id=route.id,
                )
                db.add(row)
            else:
                row.revision += 1
            row.eligible = bool(eligible)
            row.capability_fingerprint = (
                str(capability_fingerprint).strip()
                if capability_fingerprint
                else None
            )
            row.evidence = _json_mapping(evidence)
            row.last_seen_at = _utc(last_seen_at or self._clock())
            return self._detach(db, row)

    # ------------------------------------------------------------------
    # Dynamic health and account eligibility
    # ------------------------------------------------------------------

    def set_account_health(
        self,
        *,
        owner: str,
        account_id: str,
        state: str,
        cooldown_until: Optional[datetime] = None,
        quota_reset_at: Optional[datetime] = None,
        last_error_code: Optional[str] = None,
        success: bool = False,
        now: Optional[datetime] = None,
    ) -> ProviderAccountHealth:
        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(db, owner, _required(account_id, "account_id"))
            row = db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id == account.id
            ).first()
            if row is None:
                row = ProviderAccountHealth(account_id=account.id)
                db.add(row)
            else:
                row.revision += 1
            row.state = _required(state, "state").lower()
            row.cooldown_until = _utc(cooldown_until) if cooldown_until else None
            row.quota_reset_at = _utc(quota_reset_at) if quota_reset_at else None
            row.last_error_code = _safe_error_code(last_error_code)
            if success:
                row.last_success_at = timestamp
            return self._detach(db, row)

    def set_account_model_health(
        self,
        *,
        owner: str,
        account_id: str,
        model_route_id: str,
        state: str,
        cooldown_until: Optional[datetime] = None,
        quota_reset_at: Optional[datetime] = None,
        last_error_code: Optional[str] = None,
        success: bool = False,
        now: Optional[datetime] = None,
    ) -> ProviderAccountModelHealth:
        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(db, owner, _required(account_id, "account_id"))
            route = self._require_route(
                db,
                owner,
                _required(model_route_id, "model_route_id"),
            )
            if account.connection_id != route.connection_id:
                raise ProviderValidationError(
                    "account and model route belong to different connections"
                )
            row = (
                db.query(ProviderAccountModelHealth)
                .filter(
                    ProviderAccountModelHealth.account_id == account.id,
                    ProviderAccountModelHealth.model_route_id == route.id,
                )
                .first()
            )
            if row is None:
                row = ProviderAccountModelHealth(
                    account_id=account.id,
                    model_route_id=route.id,
                )
                db.add(row)
            else:
                row.revision += 1
            row.state = _required(state, "state").lower()
            row.cooldown_until = _utc(cooldown_until) if cooldown_until else None
            row.quota_reset_at = _utc(quota_reset_at) if quota_reset_at else None
            row.last_error_code = _safe_error_code(last_error_code)
            if success:
                row.last_success_at = timestamp
            return self._detach(db, row)

    @staticmethod
    def _health_available(
        state: Optional[str],
        cooldown_until: Optional[datetime],
        *,
        quota_reset_at: Optional[datetime] = None,
        now: datetime,
        model_scope: bool = False,
    ) -> bool:
        hard_states = (
            _MODEL_HARD_BLOCK_STATES if model_scope else _ACCOUNT_HARD_BLOCK_STATES
        )
        if str(state or "healthy").lower() in hard_states:
            return False
        blocked_until = max(
            (
                _utc(value)
                for value in (cooldown_until, quota_reset_at)
                if value is not None
            ),
            default=None,
        )
        return blocked_until is None or blocked_until <= now

    def _eligible_account_rows(
        self,
        db: Any,
        *,
        owner: str,
        connection: ProviderConnection,
        model_route_id: Optional[str],
        allowed_account_ids: Optional[set[str]],
        now: datetime,
    ) -> list[ProviderAccount]:
        query = db.query(ProviderAccount).filter(
            ProviderAccount.owner == owner,
            ProviderAccount.connection_id == connection.id,
            ProviderAccount.enabled.is_(True),
            ProviderAccount.deleted_at.is_(None),
        )
        if allowed_account_ids is not None:
            if not allowed_account_ids:
                return []
            query = query.filter(ProviderAccount.id.in_(allowed_account_ids))
        accounts = query.order_by(ProviderAccount.sort_order, ProviderAccount.id).all()
        if not accounts:
            return []
        account_ids = [row.id for row in accounts]
        account_health = {
            row.account_id: row
            for row in db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id.in_(account_ids)
            ).all()
        }
        entitlements: set[str] = set(account_ids)
        model_health: dict[str, ProviderAccountModelHealth] = {}
        if model_route_id is not None:
            route = self._require_route(db, owner, model_route_id)
            if route.connection_id != connection.id or not route.enabled:
                raise ProviderValidationError(
                    "model route does not belong to this enabled connection"
                )
            entitlements = {
                row.account_id
                for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                    ProviderAccountEntitlement.model_route_id == route.id,
                    ProviderAccountEntitlement.eligible.is_(True),
                ).all()
            }
            model_health = {
                row.account_id: row
                for row in db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids),
                    ProviderAccountModelHealth.model_route_id == route.id,
                ).all()
            }

        eligible: list[ProviderAccount] = []
        for account in accounts:
            if account.id not in entitlements:
                continue
            health = account_health.get(account.id)
            if health is not None and not self._health_available(
                health.state,
                health.cooldown_until,
                quota_reset_at=health.quota_reset_at,
                now=now,
            ):
                continue
            scoped_health = model_health.get(account.id)
            if scoped_health is not None and not self._health_available(
                scoped_health.state,
                scoped_health.cooldown_until,
                quota_reset_at=scoped_health.quota_reset_at,
                now=now,
                model_scope=True,
            ):
                continue
            eligible.append(account)
        return eligible

    def eligible_accounts(
        self,
        *,
        owner: str,
        connection_id: str,
        model_route_id: Optional[str] = None,
        allowed_account_ids: Optional[Iterable[str]] = None,
        now: Optional[datetime] = None,
    ) -> list[ProviderAccount]:
        owner = _owner(owner)
        allowed = (
            {_required(item, "account_id") for item in allowed_account_ids}
            if allowed_account_ids is not None
            else None
        )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            connection = self._require_connection(
                db,
                owner,
                _required(connection_id, "connection_id"),
            )
            if not connection.enabled:
                return []
            rows = self._eligible_account_rows(
                db,
                owner=owner,
                connection=connection,
                model_route_id=(
                    _required(model_route_id, "model_route_id")
                    if model_route_id
                    else None
                ),
                allowed_account_ids=allowed,
                now=timestamp,
            )
            return self._detach_many(db, rows)

    def reorder_accounts(
        self,
        *,
        owner: str,
        connection_id: str,
        account_ids: Iterable[str],
        expected_revision: int,
    ) -> tuple[ProviderConnection, list[ProviderAccount]]:
        """Replace the complete live pool order under a connection revision CAS."""

        owner = _owner(owner)
        ordered_ids = _ordered_ids(account_ids, "account_id")
        with self._transaction() as db:
            connection_id = _required(connection_id, "connection_id")
            connection = self._claim_row_revision(
                db,
                ProviderConnection,
                (
                    ProviderConnection.id == connection_id,
                    ProviderConnection.owner == owner,
                    ProviderConnection.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider connection was not found",
            )
            rows = db.query(ProviderAccount).filter(
                ProviderAccount.owner == owner,
                ProviderAccount.connection_id == connection.id,
                ProviderAccount.deleted_at.is_(None),
            ).all()
            by_id = {row.id: row for row in rows}
            if set(ordered_ids) != set(by_id):
                raise ProviderValidationError(
                    "pool order must contain every live account exactly once"
                )
            ordered_rows: list[ProviderAccount] = []
            for index, account_id in enumerate(ordered_ids):
                row = by_id[account_id]
                next_order = index * 10
                if row.sort_order != next_order:
                    row.sort_order = next_order
                    row.revision += 1
                ordered_rows.append(row)
            db.flush()
            detached_connection = self._detach(db, connection)
            return detached_connection, self._detach_many(db, ordered_rows)

    def advance_pool_cursor(
        self,
        *,
        owner: str,
        connection_id: str,
        expected_revision: int,
        model_route_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> tuple[ProviderConnection, ProviderAccount]:
        """Skip the currently queued account and expose the next eligible one."""

        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            connection_id = _required(connection_id, "connection_id")
            connection = self._claim_row_revision(
                db,
                ProviderConnection,
                (
                    ProviderConnection.id == connection_id,
                    ProviderConnection.owner == owner,
                    ProviderConnection.deleted_at.is_(None),
                ),
                expected_revision=expected_revision,
                missing_message="provider connection was not found",
            )
            candidates = self._eligible_account_rows(
                db,
                owner=owner,
                connection=connection,
                model_route_id=(
                    _required(model_route_id, "model_route_id")
                    if model_route_id is not None
                    else None
                ),
                allowed_account_ids=None,
                now=timestamp,
            )
            if not candidates:
                raise NoEligibleAccount("provider connection has no eligible accounts")
            cursor = db.query(ProviderRotationCursor).filter(
                ProviderRotationCursor.owner == owner,
                ProviderRotationCursor.connection_id == connection.id,
                ProviderRotationCursor.billing_lane == connection.billing_lane,
            ).first()
            if cursor is None:
                next_position = 1
                cursor = ProviderRotationCursor(
                    owner=owner,
                    connection_id=connection.id,
                    billing_lane=connection.billing_lane,
                    next_position=next_position,
                    revision=1,
                )
                db.add(cursor)
            else:
                current_position = int(cursor.next_position)
                current_revision = int(cursor.revision)
                next_position = current_position + 1
                updated = db.query(ProviderRotationCursor).filter(
                    ProviderRotationCursor.owner == owner,
                    ProviderRotationCursor.connection_id == connection.id,
                    ProviderRotationCursor.billing_lane == connection.billing_lane,
                    ProviderRotationCursor.revision == current_revision,
                ).update(
                    {
                        ProviderRotationCursor.next_position: next_position,
                        ProviderRotationCursor.revision: current_revision + 1,
                    },
                    synchronize_session=False,
                )
                if updated != 1:
                    raise ProviderConflict("provider rotation cursor changed; retry")
            selected = candidates[next_position % len(candidates)]
            db.flush()
            return self._detach(db, connection), self._detach(db, selected)

    # ------------------------------------------------------------------
    # Sticky root bindings and equal round robin
    # ------------------------------------------------------------------

    def bind_account(
        self,
        *,
        owner: str,
        root_operation_id: str,
        connection_id: str,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        model_route_id: Optional[str] = None,
        preferred_account_id: Optional[str] = None,
        inherited_account_id: Optional[str] = None,
        grant_id: Optional[str] = None,
        allowed_account_ids: Optional[Iterable[str]] = None,
        billing_lane: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> AccountSelection:
        owner = _owner(owner)
        root_operation_id = _required(root_operation_id, "root_operation_id")
        connection_id = _required(connection_id, "connection_id")
        preferred_account_id = (
            _required(preferred_account_id, "preferred_account_id")
            if preferred_account_id
            else None
        )
        inherited_account_id = (
            _required(inherited_account_id, "inherited_account_id")
            if inherited_account_id
            else None
        )
        grant_id = _required(grant_id, "grant_id") if grant_id else None
        allowed = (
            {_required(item, "account_id") for item in allowed_account_ids}
            if allowed_account_ids is not None
            else None
        )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            credential_owner = owner
            grant: Optional[ProviderShareGrant] = None
            if grant_id is not None:
                grant = (
                    db.query(ProviderShareGrant)
                    .filter(
                        ProviderShareGrant.id == grant_id,
                        ProviderShareGrant.recipient == owner,
                        ProviderShareGrant.state == "active",
                    )
                    .first()
                )
                if grant is None:
                    raise ShareDenied("provider share grant is not active")
                if grant.connection_id != connection_id:
                    raise ShareDenied(
                        "provider share grant belongs to a different connection"
                    )
                credential_owner = grant.owner
            connection = self._require_connection(
                db,
                credential_owner,
                connection_id,
            )
            if not connection.enabled:
                raise NoEligibleAccount("provider connection is disabled")
            requested_lane = (
                _required(billing_lane, "billing_lane")
                if billing_lane is not None
                else connection.billing_lane
            )
            if requested_lane != connection.billing_lane:
                raise BillingLaneMismatch(
                    "requested billing lane does not match the connection"
                )
            if grant is not None and grant.billing_lane != requested_lane:
                raise ShareDenied("provider share grant belongs to a different billing lane")

            resolved_route: Optional[ProviderModelRoute] = None
            if model_route_id:
                resolved_route = self._require_route(
                    db,
                    credential_owner,
                    _required(model_route_id, "model_route_id"),
                )
                if resolved_route.connection_id != connection.id:
                    raise ProviderValidationError(
                        "model route belongs to a different connection"
                    )
            elif model_id:
                resolved_route = (
                    db.query(ProviderModelRoute)
                    .filter(
                        ProviderModelRoute.owner == credential_owner,
                        ProviderModelRoute.connection_id == connection.id,
                        ProviderModelRoute.provider_model_id
                        == _required(model_id, "model_id"),
                        ProviderModelRoute.enabled.is_(True),
                        ProviderModelRoute.deleted_at.is_(None),
                    )
                    .first()
                )
                if resolved_route is None:
                    raise ProviderNotFound("provider model route was not found")
            if (
                resolved_route is not None
                and model_id is not None
                and resolved_route.provider_model_id != model_id
            ):
                raise ProviderValidationError(
                    "model ID does not match the requested model route"
                )
            wire_model_id = (
                _required(model_id, "model_id")
                if model_id
                else (
                    resolved_route.provider_model_id
                    if resolved_route is not None
                    else "unspecified"
                )
            )
            wire_provider_id = (
                _required(provider_id, "provider_id")
                if provider_id
                else connection.family_id
            )

            if grant is not None:
                account_selector = AccountSelector.parse(grant.account_selector)
                model_selector = ModelSelector.parse(grant.model_selector)
                if resolved_route is None or not model_selector.includes(resolved_route.id):
                    raise ShareDenied("requested model is outside the granted subset")
                grant_accounts = (
                    set(account_selector.account_ids)
                    if account_selector.mode == "explicit_accounts"
                    else None
                )
                if grant_accounts is not None:
                    allowed = (
                        grant_accounts
                        if allowed is None
                        else allowed.intersection(grant_accounts)
                    )

            binding = (
                db.query(ProviderOperationBinding)
                .filter(
                    ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.root_operation_id == root_operation_id,
                    ProviderOperationBinding.connection_id == connection.id,
                    ProviderOperationBinding.billing_lane == requested_lane,
                )
                .first()
            )
            if binding is not None:
                if (
                    binding.credential_owner != credential_owner
                    or binding.connection_id != connection.id
                    or binding.provider_id != wire_provider_id
                    or binding.billing_lane != requested_lane
                    or binding.model_id != wire_model_id
                ):
                    raise ProviderConflict(
                        "root operation is already bound to a different provider model"
                    )
                if binding.grant_id != grant_id:
                    raise ProviderConflict(
                        "root operation is already bound under a different grant"
                    )
                if binding.selected_account_id is None:
                    if (
                        connection.billing_lane != "local"
                        or connection.kind not in _KEYLESS_CONNECTION_KINDS
                        or binding.credential_revision is not None
                        or binding.source != "keyless"
                    ):
                        raise NoEligibleAccount(
                            "sticky keyless provider binding is invalid"
                        )
                    if preferred_account_id or inherited_account_id:
                        raise NoEligibleAccount(
                            "keyless local connections do not select accounts"
                        )
                    if allowed is not None:
                        raise ShareDenied(
                            "keyless local connection is outside the account selector"
                        )
                    return AccountSelection(
                        binding_id=binding.id,
                        binding_revision=binding.revision,
                        owner=owner,
                        root_operation_id=root_operation_id,
                        connection_id=connection.id,
                        provider_id=wire_provider_id,
                        billing_lane=requested_lane,
                        model_id=wire_model_id,
                        account_id=None,
                        account_revision=None,
                        credential_version=None,
                        source="keyless",
                        attempt=binding.attempt,
                        committed=binding.state == "committed",
                        sticky=True,
                        used_preference=False,
                        fallback_reason=None,
                    )
                account = self._require_account(
                    db,
                    binding.credential_owner,
                    binding.selected_account_id,
                )
                if account.connection_id != connection.id:
                    raise NoEligibleAccount("sticky provider binding is invalid")
                if allowed is not None and account.id not in allowed:
                    raise ShareDenied("sticky provider binding is outside the allowed pool")
                if resolved_route is not None:
                    entitlement = (
                        db.query(ProviderAccountEntitlement)
                        .filter(
                            ProviderAccountEntitlement.account_id == account.id,
                            ProviderAccountEntitlement.model_route_id
                            == resolved_route.id,
                            ProviderAccountEntitlement.eligible.is_(True),
                        )
                        .first()
                    )
                    if entitlement is None:
                        raise NoEligibleAccount(
                            "sticky account is not entitled to the requested model"
                        )
                return AccountSelection(
                    binding_id=binding.id,
                    binding_revision=binding.revision,
                    owner=owner,
                    root_operation_id=root_operation_id,
                    connection_id=connection.id,
                    provider_id=wire_provider_id,
                    billing_lane=requested_lane,
                    model_id=wire_model_id,
                    account_id=account.id,
                    account_revision=account.revision,
                    credential_version=binding.credential_revision,
                    source=binding.source,
                    attempt=binding.attempt,
                    committed=binding.state == "committed",
                    sticky=True,
                    used_preference=binding.preferred_account_id == account.id,
                    fallback_reason=binding.fallback_reason,
                )

            candidates = self._eligible_account_rows(
                db,
                owner=credential_owner,
                connection=connection,
                model_route_id=(resolved_route.id if resolved_route else None),
                allowed_account_ids=allowed,
                now=timestamp,
            )
            if not candidates:
                live_accounts = db.query(ProviderAccount.id).filter(
                    ProviderAccount.owner == credential_owner,
                    ProviderAccount.connection_id == connection.id,
                    ProviderAccount.deleted_at.is_(None),
                ).count()
                keyless = (
                    connection.billing_lane == "local"
                    and connection.kind in _KEYLESS_CONNECTION_KINDS
                    and live_accounts == 0
                )
                if keyless:
                    if preferred_account_id or inherited_account_id:
                        raise NoEligibleAccount(
                            "keyless local connections do not select accounts"
                        )
                    if allowed is not None:
                        raise ShareDenied(
                            "keyless local connection is outside the account selector"
                        )
                    binding = ProviderOperationBinding(
                        id=f"pbd_{uuid.uuid4().hex}",
                        owner=owner,
                        credential_owner=credential_owner,
                        root_operation_id=root_operation_id,
                        connection_id=connection.id,
                        provider_id=wire_provider_id,
                        billing_lane=requested_lane,
                        model_id=wire_model_id,
                        grant_id=grant_id,
                        selected_account_id=None,
                        credential_revision=None,
                        preferred_account_id=None,
                        source="keyless",
                        attempt=1,
                        revision=1,
                    )
                    db.add(binding)
                    db.flush()
                    return AccountSelection(
                        binding_id=binding.id,
                        binding_revision=binding.revision,
                        owner=owner,
                        root_operation_id=root_operation_id,
                        connection_id=connection.id,
                        provider_id=wire_provider_id,
                        billing_lane=requested_lane,
                        model_id=wire_model_id,
                        account_id=None,
                        account_revision=None,
                        credential_version=None,
                        source="keyless",
                        attempt=1,
                        committed=False,
                        sticky=False,
                        used_preference=False,
                        fallback_reason=None,
                    )
                raise NoEligibleAccount(
                    "no eligible account exists in this connection and billing lane"
                )

            selected: Optional[ProviderAccount] = None
            used_preference = False
            fallback_reason: Optional[str] = None
            source = "round_robin"
            if inherited_account_id:
                selected = next(
                    (row for row in candidates if row.id == inherited_account_id),
                    None,
                )
                if selected is None:
                    raise NoEligibleAccount(
                        "inherited account is not eligible for the requested model"
                    )
                source = "inherited"
            elif preferred_account_id:
                selected = next(
                    (row for row in candidates if row.id == preferred_account_id),
                    None,
                )
                used_preference = selected is not None
                if selected is not None:
                    source = "preferred"
                else:
                    fallback_reason = "preferred_account_unavailable"
                    source = "failover"

            if selected is None:
                cursor = (
                    db.query(ProviderRotationCursor)
                    .filter(
                        ProviderRotationCursor.owner == owner,
                        ProviderRotationCursor.connection_id == connection.id,
                        ProviderRotationCursor.billing_lane == requested_lane,
                    )
                    .first()
                )
                if cursor is None:
                    cursor = ProviderRotationCursor(
                        owner=owner,
                        connection_id=connection.id,
                        billing_lane=requested_lane,
                        next_position=0,
                        revision=1,
                    )
                    db.add(cursor)
                    position = 0
                    cursor.next_position = 1
                    cursor.revision = 2
                else:
                    position = int(cursor.next_position)
                    cursor_revision = int(cursor.revision)
                    updated = db.query(ProviderRotationCursor).filter(
                        ProviderRotationCursor.owner == owner,
                        ProviderRotationCursor.connection_id == connection.id,
                        ProviderRotationCursor.billing_lane == requested_lane,
                        ProviderRotationCursor.revision == cursor_revision,
                    ).update(
                        {
                            ProviderRotationCursor.next_position: position + 1,
                            ProviderRotationCursor.revision: cursor_revision + 1,
                        },
                        synchronize_session=False,
                    )
                    if updated != 1:
                        raise ProviderConflict(
                            "provider rotation cursor changed; retry root assignment"
                        )
                selected = candidates[position % len(candidates)]

            binding = ProviderOperationBinding(
                id=f"pbd_{uuid.uuid4().hex}",
                owner=owner,
                credential_owner=credential_owner,
                root_operation_id=root_operation_id,
                connection_id=connection.id,
                provider_id=wire_provider_id,
                billing_lane=requested_lane,
                model_id=wire_model_id,
                grant_id=grant_id,
                selected_account_id=selected.id,
                credential_revision=selected.credential_version,
                preferred_account_id=preferred_account_id,
                source=source,
                attempt=1,
                fallback_reason=fallback_reason,
                revision=1,
            )
            db.add(binding)
            health = db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id == selected.id
            ).first()
            if health is None:
                health = ProviderAccountHealth(account_id=selected.id)
                db.add(health)
            else:
                health.revision += 1
            health.last_used_at = timestamp
            db.flush()
            return AccountSelection(
                binding_id=binding.id,
                binding_revision=binding.revision,
                owner=owner,
                root_operation_id=root_operation_id,
                connection_id=connection.id,
                provider_id=wire_provider_id,
                billing_lane=requested_lane,
                model_id=wire_model_id,
                account_id=selected.id,
                account_revision=selected.revision,
                credential_version=selected.credential_version,
                source=source,
                attempt=1,
                committed=False,
                sticky=False,
                used_preference=used_preference,
                fallback_reason=fallback_reason,
            )

    def mark_binding_committed(
        self,
        *,
        owner: str,
        root_operation_id: str,
        connection_id: str,
        billing_lane: str,
        now: Optional[datetime] = None,
    ) -> ProviderOperationBinding:
        owner = _owner(owner)
        with self._transaction() as db:
            row = (
                db.query(ProviderOperationBinding)
                .filter(
                    ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.root_operation_id
                    == _required(root_operation_id, "root_operation_id"),
                    ProviderOperationBinding.connection_id
                    == _required(connection_id, "connection_id"),
                    ProviderOperationBinding.billing_lane
                    == _required(billing_lane, "billing_lane"),
                )
                .first()
            )
            if row is None:
                raise ProviderNotFound("provider operation binding was not found")
            if row.state != "committed":
                row.state = "committed"
                row.committed_at = _utc(now or self._clock())
                row.revision += 1
            return self._detach(db, row)

    def _selection_from_binding(
        self,
        db: Any,
        row: ProviderOperationBinding,
        *,
        sticky: bool,
    ) -> AccountSelection:
        account: Optional[ProviderAccount] = None
        if row.selected_account_id is not None:
            account = self._require_account(
                db,
                row.credential_owner,
                row.selected_account_id,
            )
            if account.connection_id != row.connection_id:
                raise NoEligibleAccount("provider operation binding is invalid")
        return AccountSelection(
            binding_id=row.id,
            binding_revision=int(row.revision),
            owner=row.owner,
            root_operation_id=row.root_operation_id,
            connection_id=row.connection_id,
            provider_id=row.provider_id,
            billing_lane=row.billing_lane,
            model_id=row.model_id,
            account_id=(account.id if account is not None else None),
            account_revision=(account.revision if account is not None else None),
            credential_version=(
                int(row.credential_revision)
                if row.credential_revision is not None
                else None
            ),
            source=row.source,
            attempt=int(row.attempt),
            committed=row.state == "committed",
            sticky=sticky,
            used_preference=(
                account is not None and row.preferred_account_id == account.id
            ),
            fallback_reason=row.fallback_reason,
        )

    def commit_account_binding(
        self,
        *,
        owner: str,
        binding_id: str,
        expected_revision: int,
        now: Optional[datetime] = None,
    ) -> AccountSelection:
        """Fence the first externally visible output or side effect."""

        owner = _owner(owner)
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            binding_id = _required(binding_id, "binding_id")
            query = db.query(ProviderOperationBinding).filter(
                ProviderOperationBinding.id == binding_id,
                ProviderOperationBinding.owner == owner,
            )
            row = query.first()
            if row is None:
                raise ProviderNotFound("provider operation binding was not found")
            self._require_revision(row.revision, expected_revision)
            if row.state != "committed":
                updated = query.filter(
                    ProviderOperationBinding.revision == int(expected_revision),
                    ProviderOperationBinding.state == "uncommitted",
                ).update(
                    {
                        ProviderOperationBinding.state: "committed",
                        ProviderOperationBinding.committed_at: timestamp,
                        ProviderOperationBinding.revision: int(expected_revision) + 1,
                    },
                    synchronize_session=False,
                )
                if updated != 1:
                    raise ProviderConflict(
                        "provider operation binding changed before commitment"
                    )
                db.expire_all()
                row = query.first()
                if row is None:
                    raise ProviderNotFound(
                        "provider operation binding was not found"
                    )
            return self._selection_from_binding(db, row, sticky=True)

    def record_account_attempt(
        self,
        *,
        owner: str,
        binding_id: str,
        expected_revision: int,
        account_id: Optional[str],
        outcome: str,
        retry_after_ms: Optional[int] = None,
        model_eligible: Optional[bool] = None,
        now: Optional[datetime] = None,
    ) -> AccountSelection:
        """Record one safe outcome and conservatively choose a pre-commit failover.

        Authentication, quota, and model-entitlement failures may select a
        different account before commitment. Transient, unknown, and committed
        failures never cross accounts. Every account is tried at most once in a
        failover cycle, and selection remains inside the original connection,
        billing lane, grant selector, and model entitlement.
        """

        owner = _owner(owner)
        binding_id = _required(binding_id, "binding_id")
        normalized_outcome = _required(outcome, "outcome").lower()
        allowed_outcomes = {
            "success",
            "auth",
            "quota",
            "entitlement",
            "transient",
            "unknown",
        }
        if normalized_outcome not in allowed_outcomes:
            raise ProviderValidationError("unsupported provider attempt outcome")
        if retry_after_ms is not None:
            try:
                retry_after_ms = int(retry_after_ms)
            except (TypeError, ValueError) as exc:
                raise ProviderValidationError(
                    "retry_after_ms must be an integer"
                ) from exc
            if retry_after_ms < 0:
                raise ProviderValidationError(
                    "retry_after_ms must not be negative"
                )
            retry_after_ms = min(retry_after_ms, 30 * 24 * 60 * 60 * 1000)
        timestamp = _utc(now or self._clock())
        exhausted = False
        result: Optional[AccountSelection] = None

        with self._transaction() as db:
            row = self._claim_row_revision(
                db,
                ProviderOperationBinding,
                (
                    ProviderOperationBinding.id == binding_id,
                    ProviderOperationBinding.owner == owner,
                ),
                expected_revision=expected_revision,
                missing_message="provider operation binding was not found",
            )
            connection = self._require_connection(
                db,
                row.credential_owner,
                row.connection_id,
            )
            if connection.billing_lane != row.billing_lane:
                raise BillingLaneMismatch(
                    "provider operation binding changed billing lane"
                )

            selected: Optional[ProviderAccount] = None
            supplied_account = (
                _required(account_id, "account_id") if account_id is not None else None
            )
            if row.selected_account_id is None:
                if supplied_account is not None:
                    raise ProviderValidationError(
                        "keyless provider attempt cannot name an account"
                    )
                if normalized_outcome in {"auth", "quota", "entitlement"}:
                    raise ProviderValidationError(
                        "keyless provider attempt cannot report a credential outcome"
                    )
            else:
                if supplied_account != row.selected_account_id:
                    raise ProviderConflict(
                        "provider attempt account does not match its binding"
                    )
                selected = self._require_account(
                    db,
                    row.credential_owner,
                    row.selected_account_id,
                )
                if selected.connection_id != connection.id:
                    raise ProviderConflict(
                        "provider attempt account escaped its connection"
                    )

            route = (
                db.query(ProviderModelRoute)
                .filter(
                    ProviderModelRoute.owner == row.credential_owner,
                    ProviderModelRoute.connection_id == connection.id,
                    ProviderModelRoute.provider_model_id == row.model_id,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .first()
            )
            attempt_entry: dict[str, Any] = {
                "attempt": int(row.attempt),
                "outcome": normalized_outcome,
                "at": timestamp.isoformat(),
                "committed": row.state == "committed",
            }
            if selected is not None:
                attempt_entry["account_id"] = selected.id
            if retry_after_ms is not None:
                attempt_entry["retry_after_ms"] = retry_after_ms
            row.attempts = list(row.attempts or ()) + [attempt_entry]

            if selected is not None:
                health = db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id == selected.id
                ).first()
                if health is None:
                    health = ProviderAccountHealth(account_id=selected.id)
                    db.add(health)
                else:
                    health.revision += 1
                health.last_used_at = timestamp
                if normalized_outcome == "success":
                    health.state = "healthy"
                    health.cooldown_until = None
                    health.quota_reset_at = None
                    health.last_error_code = None
                    health.last_success_at = timestamp
                elif normalized_outcome == "auth":
                    health.state = "reauth_required"
                    health.cooldown_until = None
                    health.quota_reset_at = None
                    health.last_error_code = "auth"
                elif normalized_outcome == "quota":
                    delay_ms = 60_000 if retry_after_ms is None else retry_after_ms
                    health.state = "cooldown"
                    health.cooldown_until = timestamp + timedelta(
                        milliseconds=delay_ms
                    )
                    health.quota_reset_at = health.cooldown_until
                    health.last_error_code = "quota"
                elif normalized_outcome in {"transient", "unknown"}:
                    health.last_error_code = normalized_outcome

                if normalized_outcome == "entitlement":
                    if route is None:
                        raise ProviderValidationError(
                            "entitlement outcome requires a known model route"
                        )
                    scoped = db.query(ProviderAccountModelHealth).filter(
                        ProviderAccountModelHealth.account_id == selected.id,
                        ProviderAccountModelHealth.model_route_id == route.id,
                    ).first()
                    if scoped is None:
                        scoped = ProviderAccountModelHealth(
                            account_id=selected.id,
                            model_route_id=route.id,
                        )
                        db.add(scoped)
                    else:
                        scoped.revision += 1
                    scoped.state = (
                        "healthy" if model_eligible is True else "ineligible"
                    )
                    scoped.cooldown_until = None
                    scoped.quota_reset_at = None
                    scoped.last_error_code = (
                        None if model_eligible is True else "entitlement"
                    )

            may_rotate = (
                row.state != "committed"
                and selected is not None
                and normalized_outcome in {"auth", "quota", "entitlement"}
            )
            if not may_rotate:
                if normalized_outcome not in {"success"}:
                    row.attempt += 1
                result = self._selection_from_binding(db, row, sticky=True)
            else:
                allowed_account_ids: Optional[set[str]] = None
                if row.grant_id:
                    grant = (
                        db.query(ProviderShareGrant)
                        .filter(
                            ProviderShareGrant.id == row.grant_id,
                            ProviderShareGrant.recipient == owner,
                            ProviderShareGrant.state == "active",
                        )
                        .first()
                    )
                    if grant is None:
                        raise ShareDenied(
                            "provider share grant was revoked before retry"
                        )
                    if (
                        grant.connection_id != connection.id
                        or grant.billing_lane != row.billing_lane
                    ):
                        raise ShareDenied(
                            "provider share grant changed route before retry"
                        )
                    account_selector = AccountSelector.parse(
                        grant.account_selector
                    )
                    model_selector = ModelSelector.parse(grant.model_selector)
                    if route is None or not model_selector.includes(route.id):
                        raise ShareDenied(
                            "provider model is outside the grant before retry"
                        )
                    if account_selector.mode == "explicit_accounts":
                        allowed_account_ids = set(account_selector.account_ids)

                db.flush()
                attempted_ids = {
                    str(item.get("account_id"))
                    for item in (row.attempts or ())
                    if isinstance(item, Mapping) and item.get("account_id")
                }
                candidates = self._eligible_account_rows(
                    db,
                    owner=row.credential_owner,
                    connection=connection,
                    model_route_id=(route.id if route is not None else None),
                    allowed_account_ids=allowed_account_ids,
                    now=timestamp,
                )
                candidates = [
                    candidate
                    for candidate in candidates
                    if candidate.id not in attempted_ids
                ]
                if not candidates:
                    row.fallback_reason = (
                        f"{normalized_outcome}_all_accounts_exhausted"
                    )
                    exhausted = True
                else:
                    next_account = candidates[0]
                    row.selected_account_id = next_account.id
                    row.credential_revision = next_account.credential_version
                    row.source = "failover"
                    row.attempt += 1
                    row.fallback_reason = f"{normalized_outcome}_failover"
                    next_health = db.query(ProviderAccountHealth).filter(
                        ProviderAccountHealth.account_id == next_account.id
                    ).first()
                    if next_health is None:
                        next_health = ProviderAccountHealth(
                            account_id=next_account.id
                        )
                        db.add(next_health)
                    else:
                        next_health.revision += 1
                    next_health.last_used_at = timestamp
                    result = self._selection_from_binding(
                        db,
                        row,
                        sticky=True,
                    )

        if exhausted:
            raise NoEligibleAccount(
                "every eligible provider account was attempted before commitment"
            )
        if result is None:  # pragma: no cover - defensive invariant
            raise ProviderConflict("provider attempt did not produce a binding")
        return result

    # ------------------------------------------------------------------
    # Exact credential disclosure and refresh single-flight
    # ------------------------------------------------------------------

    @staticmethod
    def _credential_lease_digest(lease_id: str) -> str:
        return keyed_digest(
            lease_id,
            context="provider-credential-use-lease",
        )

    def lease_credential(
        self,
        *,
        owner: str,
        holder_id: str,
        root_operation_id: str,
        connection_id: str,
        account_id: str,
        model_id: str,
        expected_credential_revision: int,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
        ttl_seconds: float = 30,
    ) -> CredentialLeaseGrant:
        """Disclose a credential only for its persisted exact operation binding."""

        owner = _owner(owner)
        holder_id = _required(holder_id, "holder_id")
        root_operation_id = _required(root_operation_id, "root_operation_id")
        connection_id = _required(connection_id, "connection_id")
        account_id = _required(account_id, "account_id")
        model_id = _required(model_id, "model_id")
        grant_id = _required(grant_id, "grant_id") if grant_id else None
        ttl_seconds = float(ttl_seconds)
        if ttl_seconds <= 0 or ttl_seconds > 60:
            raise ProviderValidationError(
                "credential lease TTL must be positive and at most 60 seconds"
            )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            binding = (
                db.query(ProviderOperationBinding)
                .filter(
                    ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.root_operation_id == root_operation_id,
                    ProviderOperationBinding.connection_id == connection_id,
                )
                .first()
            )
            if binding is None:
                raise ProviderNotFound("provider operation binding was not found")
            connection = self._require_connection(
                db,
                binding.credential_owner,
                connection_id,
            )
            if (
                binding.provider_id != connection.family_id
                or binding.billing_lane != connection.billing_lane
            ):
                raise ShareDenied(
                    "credential request does not match its operation binding"
                )
            if binding.model_id != model_id:
                raise ShareDenied(
                    "credential request does not match its operation binding"
                )
            if binding.selected_account_id != account_id or binding.grant_id != grant_id:
                raise ShareDenied("credential request does not match its operation binding")
            self._require_revision(
                binding.credential_revision,
                expected_credential_revision,
            )
            account = self._require_account(
                db,
                binding.credential_owner,
                account_id,
            )
            if account.connection_id != connection_id:
                raise ProviderValidationError(
                    "credential account belongs to a different connection"
                )
            self._require_revision(
                account.credential_version,
                expected_credential_revision,
            )
            route = (
                db.query(ProviderModelRoute)
                .filter(
                    ProviderModelRoute.owner == binding.credential_owner,
                    ProviderModelRoute.connection_id == connection_id,
                    ProviderModelRoute.provider_model_id == model_id,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .first()
            )
            if route is None:
                raise ProviderNotFound("provider model route was not found")
            entitlement = (
                db.query(ProviderAccountEntitlement)
                .filter(
                    ProviderAccountEntitlement.account_id == account.id,
                    ProviderAccountEntitlement.model_route_id == route.id,
                    ProviderAccountEntitlement.eligible.is_(True),
                )
                .first()
            )
            if entitlement is None:
                raise NoEligibleAccount(
                    "bound account is not entitled to the requested model"
                )
            if grant_id is not None:
                grant = (
                    db.query(ProviderShareGrant)
                    .filter(
                        ProviderShareGrant.id == grant_id,
                        ProviderShareGrant.owner == binding.credential_owner,
                        ProviderShareGrant.recipient == owner,
                        ProviderShareGrant.connection_id == connection_id,
                        ProviderShareGrant.state == "active",
                    )
                    .first()
                )
                if grant is None:
                    raise ShareDenied("provider share grant is no longer active")
                if not AccountSelector.parse(grant.account_selector).includes(account.id):
                    raise ShareDenied("credential account is outside the granted subset")
                if not ModelSelector.parse(grant.model_selector).includes(route.id):
                    raise ShareDenied("credential model is outside the granted subset")
            if not account.credential_envelope:
                raise ProviderNotFound("provider account has no credential")
            credentials = unseal_credential(
                account.credential_envelope,
                CredentialScope(
                    account.owner,
                    account.connection_id,
                    account.id,
                    account.credential_version,
                ),
            )
            lease_id = "pcl_" + secrets.token_urlsafe(32)
            expires_at = timestamp + timedelta(seconds=ttl_seconds)
            db.query(ProviderCredentialLease).filter(
                ProviderCredentialLease.expires_at <= timestamp
            ).delete(synchronize_session=False)
            db.add(
                ProviderCredentialLease(
                    lease_id_digest=self._credential_lease_digest(lease_id),
                    owner=owner,
                    credential_owner=binding.credential_owner,
                    holder_id=holder_id,
                    root_operation_id=root_operation_id,
                    binding_id=binding.id,
                    connection_id=connection_id,
                    account_id=account.id,
                    model_id=model_id,
                    grant_id=grant_id,
                    credential_revision=account.credential_version,
                    issued_at=timestamp,
                    expires_at=expires_at,
                )
            )
            db.flush()
            return CredentialLeaseGrant(
                lease_id=lease_id,
                connection_id=connection_id,
                account_id=account.id,
                credential_revision=account.credential_version,
                expires_at=expires_at,
                credentials=credentials,
            )

    def consume_credential_lease(
        self,
        *,
        owner: str,
        lease_id: str,
        holder_id: str,
        now: Optional[datetime] = None,
    ) -> CredentialLeaseGrant:
        """Consume one exact driver callback lease without broadening authority.

        The callback listener authenticates the runtime/driver identity before
        invoking this method.  This method is the durable provider-side check:
        the opaque lease must belong to the current consumer, the registered
        holder must match, the account and credential revision must still be
        live, and the lease is deleted in the same transaction before the
        plaintext credential is returned.  An expired lease returns an empty
        credential sentinel so the callback layer can emit its typed
        ``credential_expired`` response without re-unsealing a secret.
        """

        owner = _owner(owner)
        lease_id = _required(lease_id, "lease_id")
        holder_id = _required(holder_id, "holder_id")
        timestamp = _utc(now or self._clock())
        digest = self._credential_lease_digest(lease_id)
        with self._transaction() as db:
            lease = (
                db.query(ProviderCredentialLease)
                .filter(ProviderCredentialLease.lease_id_digest == digest)
                .first()
            )
            # Deliberately collapse owner/holder/missing cases to one outcome;
            # a callback caller must not be able to probe another lease.
            if (
                lease is None
                or lease.owner != owner
                or lease.holder_id != holder_id
            ):
                raise ProviderNotFound("provider credential lease was not found")

            expires_at = _utc(lease.expires_at)
            account = self._require_account(
                db,
                lease.credential_owner,
                lease.account_id,
            )
            if (
                account.connection_id != lease.connection_id
                or int(account.credential_version) != int(lease.credential_revision)
                or not account.credential_envelope
            ):
                db.delete(lease)
                raise ProviderNotFound("provider credential lease is unavailable")

            # Consume before unsealing/returning: a second callback request
            # cannot recover the same provider secret even if the caller keeps
            # the first response alive.
            db.delete(lease)
            if expires_at <= timestamp:
                return CredentialLeaseGrant(
                    lease_id=lease_id,
                    connection_id=lease.connection_id,
                    account_id=lease.account_id,
                    credential_revision=lease.credential_revision,
                    expires_at=expires_at,
                    credentials={},
                )

            credentials = unseal_credential(
                account.credential_envelope,
                CredentialScope(
                    account.owner,
                    account.connection_id,
                    account.id,
                    account.credential_version,
                ),
            )
            return CredentialLeaseGrant(
                lease_id=lease_id,
                connection_id=lease.connection_id,
                account_id=lease.account_id,
                credential_revision=lease.credential_revision,
                expires_at=expires_at,
                credentials=credentials,
            )

    @staticmethod
    def _lease_digest(token: str) -> str:
        return keyed_digest(
            token,
            context="provider-refresh-lease",
        )

    def acquire_refresh_lease(
        self,
        *,
        owner: str,
        account_id: str,
        holder_id: str,
        connection_id: Optional[str] = None,
        expected_credential_revision: Optional[int] = None,
        now: Optional[datetime] = None,
        ttl_seconds: float = 60,
    ) -> RefreshLeaseGrant:
        owner = _owner(owner)
        account_id = _required(account_id, "account_id")
        holder_id = _required(holder_id, "holder_id")
        ttl_seconds = float(ttl_seconds)
        if ttl_seconds <= 0 or ttl_seconds > 60:
            raise ProviderValidationError(
                "refresh lease TTL must be positive and at most 60 seconds"
            )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(db, owner, account_id)
            if connection_id is not None and account.connection_id != _required(
                connection_id,
                "connection_id",
            ):
                raise ProviderValidationError(
                    "refresh account belongs to a different connection"
                )
            if expected_credential_revision is not None:
                self._require_revision(
                    account.credential_version,
                    expected_credential_revision,
                )
            existing = db.query(ProviderRefreshLease).filter(
                ProviderRefreshLease.account_id == account.id
            ).first()
            if existing is not None and _utc(existing.expires_at) > timestamp:
                raise RefreshLeaseBusy("provider account refresh is already leased")
            if existing is not None:
                db.delete(existing)
                db.flush()
            token = secrets.token_urlsafe(32)
            expires_at = timestamp + timedelta(seconds=ttl_seconds)
            row = ProviderRefreshLease(
                account_id=account.id,
                holder_id=holder_id,
                token_digest=self._lease_digest(token),
                credential_revision=account.credential_version,
                acquired_at=timestamp,
                expires_at=expires_at,
                max_expires_at=timestamp + timedelta(seconds=120),
                renewals=0,
            )
            db.add(row)
            db.flush()
            return RefreshLeaseGrant(
                connection_id=account.connection_id,
                account_id=account.id,
                holder_id=holder_id,
                credential_revision=account.credential_version,
                expires_at=expires_at,
                renewable=True,
                token=token,
            )

    def _require_lease(
        self,
        db: Any,
        *,
        token: str,
        now: datetime,
        account_id: Optional[str] = None,
    ) -> ProviderRefreshLease:
        supplied = self._lease_digest(_required(token, "lease token"))
        row = db.query(ProviderRefreshLease).filter(
            ProviderRefreshLease.token_digest == supplied
        ).first()
        if (
            row is None
            or not hmac.compare_digest(row.token_digest, supplied)
            or (account_id is not None and row.account_id != account_id)
        ):
            raise RefreshLeaseInvalid("provider refresh lease is invalid")
        if _utc(row.expires_at) <= now:
            raise RefreshLeaseInvalid("provider refresh lease has expired")
        return row

    def renew_refresh_lease(
        self,
        *,
        owner: str,
        account_id: Optional[str] = None,
        token: str,
        now: Optional[datetime] = None,
        ttl_seconds: float = 60,
    ) -> RefreshLeaseGrant:
        owner = _owner(owner)
        account_id = (
            _required(account_id, "account_id") if account_id is not None else None
        )
        ttl_seconds = float(ttl_seconds)
        if ttl_seconds <= 0 or ttl_seconds > 60:
            raise ProviderValidationError(
                "refresh lease TTL must be positive and at most 60 seconds"
            )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            row = self._require_lease(
                db,
                token=token,
                now=timestamp,
                account_id=account_id,
            )
            account = self._require_account(db, owner, row.account_id)
            if row.renewals >= 1:
                raise RefreshLeaseInvalid("provider refresh lease was already renewed")
            row.expires_at = min(
                timestamp + timedelta(seconds=ttl_seconds),
                _utc(row.max_expires_at),
            )
            row.renewals += 1
            db.flush()
            return RefreshLeaseGrant(
                connection_id=account.connection_id,
                account_id=account.id,
                holder_id=row.holder_id,
                credential_revision=row.credential_revision,
                expires_at=row.expires_at,
                renewable=False,
                token=token,
            )

    def commit_refresh(
        self,
        *,
        owner: str,
        account_id: str,
        token: str,
        expected_credential_revision: int,
        credentials: Mapping[str, Any],
        now: Optional[datetime] = None,
    ) -> ProviderAccount:
        owner = _owner(owner)
        account_id = _required(account_id, "account_id")
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(db, owner, account_id)
            previous_credential_revision = int(account.credential_version)
            lease = self._require_lease(
                db,
                token=token,
                now=timestamp,
                account_id=account.id,
            )
            self._require_revision(
                lease.credential_revision,
                expected_credential_revision,
            )
            account = self._install_credential(
                db,
                account,
                credentials,
                expected_credential_version=expected_credential_revision,
            )
            next_credential_revision = int(account.credential_version)
            # A refresh occurs inside an already-selected operation. Advance
            # every matching *uncommitted* affinity in the same transaction so
            # the refreshed worker can re-lease or report its outcome without
            # tripping over the credential CAS it just committed. Binding
            # revision is deliberately unchanged: selection did not change,
            # and the engine still holds that binding revision in memory.
            db.query(ProviderOperationBinding).filter(
                ProviderOperationBinding.credential_owner == account.owner,
                ProviderOperationBinding.connection_id == account.connection_id,
                ProviderOperationBinding.selected_account_id == account.id,
                ProviderOperationBinding.credential_revision
                == previous_credential_revision,
                ProviderOperationBinding.state == "uncommitted",
            ).update(
                {
                    ProviderOperationBinding.credential_revision:
                    next_credential_revision,
                },
                synchronize_session=False,
            )
            # Old short-lived disclosures are no longer valid after the CAS.
            db.query(ProviderCredentialLease).filter(
                ProviderCredentialLease.account_id == account.id,
                ProviderCredentialLease.credential_revision
                == previous_credential_revision,
            ).delete(synchronize_session=False)
            db.delete(lease)
            return self._detach(db, account)

    def abort_refresh(
        self,
        *,
        owner: str,
        account_id: Optional[str] = None,
        token: str,
        now: Optional[datetime] = None,
    ) -> None:
        owner = _owner(owner)
        account_id = (
            _required(account_id, "account_id") if account_id is not None else None
        )
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            row = self._require_lease(
                db,
                token=token,
                now=timestamp,
                account_id=account_id,
            )
            self._require_account(db, owner, row.account_id)
            db.delete(row)

    # ------------------------------------------------------------------
    # Durable purpose defaults and fallbacks
    # ------------------------------------------------------------------

    def list_route_bindings(self, *, owner: str) -> list[ProviderRouteBinding]:
        owner = _owner(owner)
        with self._transaction() as db:
            rows = db.query(ProviderRouteBinding).filter(
                ProviderRouteBinding.owner == owner,
            ).order_by(
                ProviderRouteBinding.purpose,
                ProviderRouteBinding.ordinal,
                ProviderRouteBinding.id,
            ).all()
            return self._detach_many(db, rows)

    def get_route_bindings_for_purpose(
        self,
        *,
        owner: str,
        purpose: str,
    ) -> list[ProviderRouteBinding]:
        owner = _owner(owner)
        purpose = _required(purpose, "purpose").lower()
        with self._transaction() as db:
            rows = db.query(ProviderRouteBinding).filter(
                ProviderRouteBinding.owner == owner,
                ProviderRouteBinding.purpose == purpose,
            ).order_by(
                ProviderRouteBinding.ordinal,
                ProviderRouteBinding.id,
            ).all()
            if not rows:
                raise ProviderNotFound("provider route binding was not found")
            return self._detach_many(db, rows)

    def put_route_binding(
        self,
        *,
        owner: str,
        purpose: str,
        model_route_ids: Iterable[str],
        expected_revision: int,
        enabled_by_route: Optional[Mapping[str, bool]] = None,
    ) -> list[ProviderRouteBinding]:
        owner = _owner(owner)
        purpose = _required(purpose, "purpose").lower()
        route_ids = _ordered_ids(model_route_ids, "model_route_id")
        enabled_map = {
            _required(key, "model_route_id"): bool(value)
            for key, value in dict(enabled_by_route or {}).items()
        }
        if set(enabled_map) - set(route_ids):
            raise ProviderValidationError(
                "route binding enabled map contains an unknown route"
            )
        with self._transaction() as db:
            routes = db.query(ProviderModelRoute.id).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.id.in_(route_ids),
                ProviderModelRoute.enabled.is_(True),
                ProviderModelRoute.deleted_at.is_(None),
            ).all()
            if {value for value, in routes} != set(route_ids):
                raise ProviderValidationError(
                    "route binding contains an unavailable model route"
                )
            existing = db.query(ProviderRouteBinding).filter(
                ProviderRouteBinding.owner == owner,
                ProviderRouteBinding.purpose == purpose,
            ).order_by(ProviderRouteBinding.ordinal).all()
            current_revision = max(
                (int(row.revision) for row in existing),
                default=0,
            )
            if not existing:
                if int(expected_revision) != 0:
                    raise ProviderRevisionConflict(
                        expected=expected_revision,
                        current=0,
                    )
            else:
                self._require_revision(current_revision, expected_revision)
                for row in existing:
                    db.delete(row)
                db.flush()
            revision = current_revision + 1
            rows = [
                ProviderRouteBinding(
                    id=f"prb_{uuid.uuid4().hex}",
                    owner=owner,
                    purpose=purpose,
                    ordinal=ordinal,
                    model_route_id=route_id,
                    enabled=enabled_map.get(route_id, True),
                    revision=revision,
                )
                for ordinal, route_id in enumerate(route_ids)
            ]
            db.add_all(rows)
            db.flush()
            return self._detach_many(db, rows)

    def delete_route_binding(
        self,
        *,
        owner: str,
        purpose: str,
        expected_revision: int,
    ) -> None:
        owner = _owner(owner)
        purpose = _required(purpose, "purpose").lower()
        with self._transaction() as db:
            rows = db.query(ProviderRouteBinding).filter(
                ProviderRouteBinding.owner == owner,
                ProviderRouteBinding.purpose == purpose,
            ).all()
            if not rows:
                raise ProviderNotFound("provider route binding was not found")
            current_revision = max(int(row.revision) for row in rows)
            self._require_revision(current_revision, expected_revision)
            for row in rows:
                db.delete(row)

    # ------------------------------------------------------------------
    # Granular account x model sharing
    # ------------------------------------------------------------------

    @staticmethod
    def _disclosures(values: Iterable[str]) -> tuple[str, ...]:
        fields = tuple(sorted({_required(item, "disclosure field") for item in values}))
        unknown = set(fields) - SAFE_DISCLOSURE_FIELDS
        if unknown:
            raise ProviderValidationError("share disclosure field is not permitted")
        return fields

    def _validate_selector_members(
        self,
        db: Any,
        *,
        owner: str,
        connection_id: str,
        account_selector: AccountSelector,
        model_selector: ModelSelector,
    ) -> None:
        if account_selector.mode == "explicit_accounts":
            count = (
                db.query(ProviderAccount.id)
                .filter(
                    ProviderAccount.owner == owner,
                    ProviderAccount.connection_id == connection_id,
                    ProviderAccount.id.in_(account_selector.account_ids),
                    ProviderAccount.deleted_at.is_(None),
                )
                .count()
            )
            if count != len(account_selector.account_ids):
                raise ProviderValidationError(
                    "explicit account selector contains an invalid account"
                )
        if model_selector.mode == "explicit_models":
            count = (
                db.query(ProviderModelRoute.id)
                .filter(
                    ProviderModelRoute.owner == owner,
                    ProviderModelRoute.connection_id == connection_id,
                    ProviderModelRoute.id.in_(model_selector.model_route_ids),
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .count()
            )
            if count != len(model_selector.model_route_ids):
                raise ProviderValidationError(
                    "explicit model selector contains an invalid route"
                )

    def get_share_grant(
        self,
        *,
        grant_id: str,
        owner: Optional[str] = None,
        recipient: Optional[str] = None,
    ) -> ProviderShareGrant:
        if (owner is None) == (recipient is None):
            raise ProviderValidationError(
                "exactly one of owner or recipient is required"
            )
        with self._transaction() as db:
            query = db.query(ProviderShareGrant).filter(
                ProviderShareGrant.id == _required(grant_id, "grant_id"),
            )
            if owner is not None:
                query = query.filter(ProviderShareGrant.owner == _owner(owner))
            else:
                query = query.filter(
                    ProviderShareGrant.recipient == _owner(recipient),
                )
            row = query.first()
            if row is None:
                raise ProviderNotFound("provider share grant was not found")
            return self._detach(db, row)

    def list_share_grants(
        self,
        *,
        owner: Optional[str] = None,
        recipient: Optional[str] = None,
        include_revoked: bool = False,
    ) -> list[ProviderShareGrant]:
        if (owner is None) == (recipient is None):
            raise ProviderValidationError(
                "exactly one of owner or recipient is required"
            )
        with self._transaction() as db:
            query = db.query(ProviderShareGrant)
            if owner is not None:
                query = query.filter(ProviderShareGrant.owner == _owner(owner))
            else:
                query = query.filter(
                    ProviderShareGrant.recipient == _owner(recipient),
                )
            if not include_revoked:
                query = query.filter(ProviderShareGrant.state == "active")
            rows = query.order_by(
                ProviderShareGrant.created_at,
                ProviderShareGrant.id,
            ).all()
            return self._detach_many(db, rows)

    def create_share_grant(
        self,
        *,
        owner: str,
        recipient: str,
        connection_id: str,
        account_selector: AccountSelector | Mapping[str, Any],
        model_selector: ModelSelector | Mapping[str, Any],
        disclosure_fields: Iterable[str] = (),
        label: str = "Shared provider",
        grant_id: Optional[str] = None,
    ) -> ProviderShareGrant:
        owner = _owner(owner)
        recipient = _owner(recipient)
        if recipient == owner:
            raise ProviderValidationError("provider share recipient must differ from owner")
        connection_id = _required(connection_id, "connection_id")
        account_selector = AccountSelector.parse(account_selector)
        model_selector = ModelSelector.parse(model_selector)
        disclosures = self._disclosures(disclosure_fields)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, connection_id)
            self._validate_selector_members(
                db,
                owner=owner,
                connection_id=connection.id,
                account_selector=account_selector,
                model_selector=model_selector,
            )
            row = ProviderShareGrant(
                id=grant_id or f"psg_{uuid.uuid4().hex}",
                owner=owner,
                recipient=recipient,
                connection_id=connection.id,
                billing_lane=connection.billing_lane,
                label=_required(label, "label"),
                account_selector=account_selector.as_json(),
                model_selector=model_selector.as_json(),
                disclosure_fields=list(disclosures),
                revision=1,
                accepted_revision=1,
            )
            db.add(row)
            return self._detach(db, row)

    def set_model_share(
        self,
        *,
        owner: str,
        recipient: str,
        connection_id: str,
        model_route_id: str,
        enabled: bool,
    ) -> Optional[ProviderShareGrant]:
        """Set one recipient's effective access to one owned model route.

        Simple model toggles aggregate into an all-live-account, explicit-model
        grant.  Existing granular grants remain authoritative: enabling is a
        no-op when any active grant already includes the model, while disabling
        removes the model from every active grant that includes it.
        """

        owner = _owner(owner)
        recipient = _owner(recipient)
        if recipient == owner:
            raise ProviderValidationError("provider share recipient must differ from owner")
        connection_id = _required(connection_id, "connection_id")
        model_route_id = _required(model_route_id, "model_route_id")
        if not isinstance(enabled, bool):
            raise ProviderValidationError("enabled must be a boolean")

        canonical_id = self.deterministic_resource_id(
            prefix="psg",
            owner=owner,
            operation="shares.model-toggle",
            idempotency_key=json.dumps(
                [recipient, connection_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        with self._transaction() as db:
            connection = self._require_connection(
                db,
                owner,
                connection_id,
                require_live=enabled,
            )
            route = self._require_route(
                db,
                owner,
                model_route_id,
                require_live=enabled,
            )
            if route.connection_id != connection.id:
                raise ProviderValidationError(
                    "model route belongs to a different connection"
                )
            if enabled and (not connection.enabled or not route.enabled):
                raise ProviderValidationError(
                    "disabled provider models cannot be shared"
                )

            rows = (
                db.query(ProviderShareGrant)
                .filter(
                    ProviderShareGrant.owner == owner,
                    ProviderShareGrant.recipient == recipient,
                    ProviderShareGrant.connection_id == connection.id,
                )
                .order_by(ProviderShareGrant.created_at, ProviderShareGrant.id)
                .all()
            )
            active: list[
                tuple[ProviderShareGrant, AccountSelector, ModelSelector]
            ] = []
            for row in rows:
                if row.state != "active":
                    continue
                active.append(
                    (
                        row,
                        AccountSelector.parse(row.account_selector),
                        ModelSelector.parse(row.model_selector),
                    )
                )

            including = [
                item for item in active if item[2].includes(route.id)
            ]
            if enabled and including:
                return self._detach(db, including[0][0])

            if enabled:
                by_id = db.query(ProviderShareGrant).filter(
                    ProviderShareGrant.id == canonical_id,
                ).first()
                target: Optional[ProviderShareGrant] = None
                if by_id is not None:
                    if (
                        by_id.owner != owner
                        or by_id.recipient != recipient
                        or by_id.connection_id != connection.id
                        or by_id.billing_lane != connection.billing_lane
                        or AccountSelector.parse(by_id.account_selector).mode
                        != "all_live_accounts"
                        or ModelSelector.parse(by_id.model_selector).mode
                        != "explicit_models"
                    ):
                        raise ProviderConflict(
                            "deterministic model-share ID conflicts with another grant"
                        )
                    target = by_id
                else:
                    compatible = [
                        row
                        for row, account_selector, model_selector in active
                        if account_selector.mode == "all_live_accounts"
                        and model_selector.mode == "explicit_models"
                        and row.billing_lane == connection.billing_lane
                    ]
                    if compatible:
                        target = compatible[0]
                    else:
                        for row in rows:
                            if row.state == "active":
                                continue
                            account_selector = AccountSelector.parse(
                                row.account_selector
                            )
                            model_selector = ModelSelector.parse(row.model_selector)
                            if (
                                account_selector.mode == "all_live_accounts"
                                and model_selector.mode == "explicit_models"
                                and row.billing_lane == connection.billing_lane
                            ):
                                target = row
                                break

                if target is None:
                    row = ProviderShareGrant(
                        id=canonical_id,
                        owner=owner,
                        recipient=recipient,
                        connection_id=connection.id,
                        billing_lane=connection.billing_lane,
                        label="Shared models",
                        account_selector=AccountSelector.all_live().as_json(),
                        model_selector=ModelSelector.explicit([route.id]).as_json(),
                        disclosure_fields=[],
                        state="active",
                        revision=1,
                        accepted_revision=1,
                    )
                    db.add(row)
                    return self._detach(db, row)

                previous_state = target.state
                target = self._claim_row_revision(
                    db,
                    ProviderShareGrant,
                    (
                        ProviderShareGrant.id == target.id,
                        ProviderShareGrant.owner == owner,
                        ProviderShareGrant.recipient == recipient,
                        ProviderShareGrant.connection_id == connection.id,
                    ),
                    expected_revision=int(target.revision),
                    missing_message="provider share grant was not found",
                )
                existing_models = (
                    set(ModelSelector.parse(target.model_selector).model_route_ids)
                    if previous_state == "active"
                    else set()
                )
                existing_models.add(route.id)
                target.model_selector = ModelSelector.explicit(
                    existing_models
                ).as_json()
                target.state = "active"
                target.accepted_revision = target.revision
                return self._detach(db, target)

            if not including:
                if not rows:
                    return None
                preferred = next(
                    (row for row in rows if row.id == canonical_id),
                    rows[0],
                )
                return self._detach(db, preferred)

            other_live_route_ids = {
                value
                for value, in db.query(ProviderModelRoute.id)
                .filter(
                    ProviderModelRoute.owner == owner,
                    ProviderModelRoute.connection_id == connection.id,
                    ProviderModelRoute.id != route.id,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                )
                .all()
            }
            mutations = [
                (row.id, int(row.revision), model_selector)
                for row, _account_selector, model_selector in including
            ]
            result_id = mutations[0][0]
            for grant_id, revision, model_selector in mutations:
                row = self._claim_row_revision(
                    db,
                    ProviderShareGrant,
                    (
                        ProviderShareGrant.id == grant_id,
                        ProviderShareGrant.owner == owner,
                        ProviderShareGrant.recipient == recipient,
                        ProviderShareGrant.connection_id == connection.id,
                        ProviderShareGrant.state == "active",
                    ),
                    expected_revision=revision,
                    missing_message="provider share grant was not found",
                )
                remaining = (
                    set(model_selector.model_route_ids) - {route.id}
                    if model_selector.mode == "explicit_models"
                    else set(other_live_route_ids)
                )
                if remaining:
                    row.model_selector = ModelSelector.explicit(remaining).as_json()
                else:
                    row.state = "revoked"
                row.accepted_revision = row.revision
                db.flush()

            result = db.query(ProviderShareGrant).filter(
                ProviderShareGrant.id == result_id,
            ).first()
            if result is None:  # pragma: no cover - defensive invariant
                raise ProviderConflict("provider share mutation lost its grant")
            return self._detach(db, result)

    def replace_share_selectors(
        self,
        *,
        owner: str,
        grant_id: str,
        expected_revision: int,
        account_selector: AccountSelector | Mapping[str, Any],
        model_selector: ModelSelector | Mapping[str, Any],
        disclosure_fields: Iterable[str] = (),
    ) -> ProviderShareGrant:
        owner = _owner(owner)
        account_selector = AccountSelector.parse(account_selector)
        model_selector = ModelSelector.parse(model_selector)
        disclosures = set(self._disclosures(disclosure_fields))
        with self._transaction() as db:
            grant_id = _required(grant_id, "grant_id")
            row = self._claim_row_revision(
                db,
                ProviderShareGrant,
                (
                    ProviderShareGrant.id == _required(grant_id, "grant_id"),
                    ProviderShareGrant.owner == owner,
                    ProviderShareGrant.state == "active",
                ),
                expected_revision=expected_revision,
                missing_message="provider share grant was not found",
            )
            self._validate_selector_members(
                db,
                owner=owner,
                connection_id=row.connection_id,
                account_selector=account_selector,
                model_selector=model_selector,
            )
            row.account_selector = account_selector.as_json()
            row.model_selector = model_selector.as_json()
            row.disclosure_fields = sorted(disclosures)
            row.accepted_revision = row.revision
            return self._detach(db, row)

    def revoke_share_grant(
        self,
        *,
        owner: str,
        grant_id: str,
        expected_revision: int,
    ) -> ProviderShareGrant:
        owner = _owner(owner)
        with self._transaction() as db:
            grant_id = _required(grant_id, "grant_id")
            row = self._claim_row_revision(
                db,
                ProviderShareGrant,
                (
                    ProviderShareGrant.id == _required(grant_id, "grant_id"),
                    ProviderShareGrant.owner == owner,
                ),
                expected_revision=expected_revision,
                missing_message="provider share grant was not found",
            )
            row.state = "revoked"
            return self._detach(db, row)

    def resolve_share_scope(
        self,
        *,
        recipient: str,
        grant_id: str,
    ) -> ShareScope:
        recipient = _owner(recipient)
        with self._transaction() as db:
            row = (
                db.query(ProviderShareGrant)
                .filter(
                    ProviderShareGrant.id == _required(grant_id, "grant_id"),
                    ProviderShareGrant.recipient == recipient,
                    ProviderShareGrant.state == "active",
                )
                .first()
            )
            if row is None:
                raise ShareDenied("provider share grant is not active for this recipient")
            connection = self._require_connection(db, row.owner, row.connection_id)
            if not connection.enabled or connection.billing_lane != row.billing_lane:
                raise ShareDenied("provider share connection is unavailable")
            account_selector = AccountSelector.parse(row.account_selector)
            model_selector = ModelSelector.parse(row.model_selector)

            account_query = db.query(ProviderAccount.id).filter(
                ProviderAccount.owner == row.owner,
                ProviderAccount.connection_id == row.connection_id,
                ProviderAccount.enabled.is_(True),
                ProviderAccount.deleted_at.is_(None),
            )
            if account_selector.mode == "explicit_accounts":
                account_query = account_query.filter(
                    ProviderAccount.id.in_(account_selector.account_ids)
                )
            account_ids = tuple(sorted(value for value, in account_query.all()))

            model_query = db.query(ProviderModelRoute.id).filter(
                ProviderModelRoute.owner == row.owner,
                ProviderModelRoute.connection_id == row.connection_id,
                ProviderModelRoute.enabled.is_(True),
                ProviderModelRoute.deleted_at.is_(None),
            )
            if model_selector.mode == "explicit_models":
                model_query = model_query.filter(
                    ProviderModelRoute.id.in_(model_selector.model_route_ids)
                )
            model_ids = tuple(sorted(value for value, in model_query.all()))

            return ShareScope(
                grant_id=row.id,
                owner=row.owner,
                recipient=row.recipient,
                connection_id=row.connection_id,
                billing_lane=row.billing_lane,
                account_ids=account_ids,
                model_route_ids=model_ids,
                disclosure_fields=tuple(sorted(row.disclosure_fields or ())),
                # Recipient account pins are retired. Shared calls rotate over
                # the owner's eligible authentication pool for this model.
                preferred_account_id=None,
                revision=row.revision,
            )

    def assert_share_selection(
        self,
        *,
        recipient: str,
        grant_id: str,
        account_id: str,
        model_route_id: str,
    ) -> ShareScope:
        scope = self.resolve_share_scope(
            recipient=recipient,
            grant_id=grant_id,
        )
        account_id = _required(account_id, "account_id")
        model_route_id = _required(model_route_id, "model_route_id")
        if account_id not in scope.account_ids or model_route_id not in scope.model_route_ids:
            raise ShareDenied("provider selection is outside the granted subset")
        with self._transaction() as db:
            entitlement = (
                db.query(ProviderAccountEntitlement)
                .filter(
                    ProviderAccountEntitlement.account_id == account_id,
                    ProviderAccountEntitlement.model_route_id == model_route_id,
                    ProviderAccountEntitlement.eligible.is_(True),
                )
                .first()
            )
            if entitlement is None:
                raise ShareDenied("granted account is not entitled to the selected model")
        return scope

    def _received_share_projections_in_db(
        self,
        db: Any,
        *,
        recipient: str,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[ReceivedShareProjection]:
        """Build every received-share display row with a fixed query budget."""

        query = db.query(ProviderShareGrant).filter(
            ProviderShareGrant.recipient == recipient,
            ProviderShareGrant.state == "active",
        )
        if grant_id is not None:
            query = query.filter(ProviderShareGrant.id == grant_id)
        grants = query.order_by(
            ProviderShareGrant.created_at,
            ProviderShareGrant.id,
        ).all()
        if grant_id is not None and not grants:
            raise ShareDenied("provider share grant is not active for this recipient")
        if not grants:
            return []

        connection_ids = sorted({row.connection_id for row in grants})
        connections = db.query(ProviderConnection).filter(
            ProviderConnection.id.in_(connection_ids),
        ).all()
        connection_by_id = {row.id: row for row in connections}
        for grant in grants:
            connection = connection_by_id.get(grant.connection_id)
            if (
                connection is None
                or connection.owner != grant.owner
                or connection.deleted_at is not None
                or not connection.enabled
                or connection.billing_lane != grant.billing_lane
            ):
                raise ShareDenied("provider share connection is unavailable")

        accounts = db.query(ProviderAccount).filter(
            ProviderAccount.connection_id.in_(connection_ids),
            ProviderAccount.enabled.is_(True),
            ProviderAccount.deleted_at.is_(None),
        ).order_by(ProviderAccount.id).all()
        routes = db.query(ProviderModelRoute).filter(
            ProviderModelRoute.connection_id.in_(connection_ids),
            ProviderModelRoute.enabled.is_(True),
            ProviderModelRoute.deleted_at.is_(None),
        ).order_by(ProviderModelRoute.id).all()
        accounts_by_connection: dict[tuple[str, str], list[ProviderAccount]] = {}
        for account in accounts:
            accounts_by_connection.setdefault(
                (account.owner, account.connection_id), []
            ).append(account)
        routes_by_connection: dict[tuple[str, str], list[ProviderModelRoute]] = {}
        for route in routes:
            routes_by_connection.setdefault(
                (route.owner, route.connection_id), []
            ).append(route)

        needs_health = any(
            "detailed_health" in set(grant.disclosure_fields or ())
            for grant in grants
        )
        pair_health: dict[tuple[str, str], dict[str, Any]] = {}
        if needs_health and accounts and routes:
            account_ids = [row.id for row in accounts]
            route_ids = [row.id for row in routes]
            entitlements = {
                (row.model_route_id, row.account_id): row
                for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                    ProviderAccountEntitlement.model_route_id.in_(route_ids),
                ).all()
            }
            account_health = {
                row.account_id: row
                for row in db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_(account_ids),
                ).all()
            }
            model_health = {
                (row.model_route_id, row.account_id): row
                for row in db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids),
                    ProviderAccountModelHealth.model_route_id.in_(route_ids),
                ).all()
            }
            timestamp = _utc(now or self._clock())
            for route in routes:
                for account in accounts_by_connection.get(
                    (route.owner, route.connection_id), ()
                ):
                    entitlement = entitlements.get((route.id, account.id))
                    whole = account_health.get(account.id)
                    scoped = model_health.get((route.id, account.id))
                    entitled = bool(entitlement and entitlement.eligible)
                    eligible = (
                        entitled
                        and (
                            whole is None
                            or self._health_available(
                                whole.state,
                                whole.cooldown_until,
                                quota_reset_at=whole.quota_reset_at,
                                now=timestamp,
                            )
                        )
                        and (
                            scoped is None
                            or self._health_available(
                                scoped.state,
                                scoped.cooldown_until,
                                quota_reset_at=scoped.quota_reset_at,
                                now=timestamp,
                                model_scope=True,
                            )
                        )
                    )
                    pair_health[(route.id, account.id)] = {
                        "model_route_id": route.id,
                        "eligible": eligible,
                        "account_state": str(
                            getattr(whole, "state", None) or "healthy"
                        ),
                        "model_state": str(
                            getattr(scoped, "state", None) or "healthy"
                        ),
                        "account_cooldown_until": getattr(
                            whole, "cooldown_until", None
                        ),
                        "model_cooldown_until": getattr(
                            scoped, "cooldown_until", None
                        ),
                        "account_last_error_code": getattr(
                            whole, "last_error_code", None
                        ),
                        "model_last_error_code": getattr(
                            scoped, "last_error_code", None
                        ),
                    }

        projections: list[ReceivedShareProjection] = []
        for grant in grants:
            account_selector = AccountSelector.parse(grant.account_selector)
            model_selector = ModelSelector.parse(grant.model_selector)
            selected_accounts = tuple(
                account
                for account in accounts_by_connection.get(
                    (grant.owner, grant.connection_id), ()
                )
                if account_selector.includes(account.id)
            )
            selected_routes = tuple(
                route
                for route in routes_by_connection.get(
                    (grant.owner, grant.connection_id), ()
                )
                if model_selector.includes(route.id)
            )
            health_by_account: dict[str, tuple[Mapping[str, Any], ...]] = {}
            if "detailed_health" in set(grant.disclosure_fields or ()):
                health_by_account = {
                    account.id: tuple(
                        pair_health[(route.id, account.id)]
                        for route in selected_routes
                        if (route.id, account.id) in pair_health
                    )
                    for account in selected_accounts
                }
            projections.append(
                ReceivedShareProjection(
                    grant=grant,
                    connection=connection_by_id[grant.connection_id],
                    accounts=selected_accounts,
                    model_routes=selected_routes,
                    health_by_account=health_by_account,
                )
            )

        # A row may participate in many grants. Expunge each identity once so
        # callers can serialize after the read transaction closes.
        for row in (*grants, *connections, *accounts, *routes):
            db.expunge(row)
        return projections

    def received_share_projections(
        self,
        *,
        recipient: str,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> list[ReceivedShareProjection]:
        """Return one or all active recipient projections without N+1 reads."""

        recipient = _owner(recipient)
        grant_id = (
            _required(grant_id, "grant_id") if grant_id is not None else None
        )
        with self._transaction() as db:
            return self._received_share_projections_in_db(
                db,
                recipient=recipient,
                grant_id=grant_id,
                now=now,
            )

    def management_snapshot(self, *, owner: str) -> ProviderManagementSnapshot:
        """Read the complete nonsecret settings projection in one transaction.

        This intentionally has no process cache: every response reflects the
        latest committed mutation or share revocation, so display freshness
        never becomes authorization authority and no invalidation matrix is
        required.
        """

        owner = _owner(owner)
        with self._transaction() as db:
            connections = db.query(ProviderConnection).filter(
                ProviderConnection.owner == owner,
                ProviderConnection.deleted_at.is_(None),
            ).order_by(ProviderConnection.created_at, ProviderConnection.id).all()
            account_counts = {
                connection_id: int(count)
                for connection_id, count in db.query(
                    ProviderAccount.connection_id,
                    func.count(ProviderAccount.id),
                ).filter(
                    ProviderAccount.owner == owner,
                    ProviderAccount.deleted_at.is_(None),
                ).group_by(ProviderAccount.connection_id).all()
            }
            model_routes = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.deleted_at.is_(None),
            ).order_by(
                ProviderModelRoute.connection_id,
                ProviderModelRoute.display_name,
                ProviderModelRoute.id,
            ).all()
            received_shares = self._received_share_projections_in_db(
                db,
                recipient=owner,
            )
            for row in (
                *connections,
                *model_routes,
            ):
                db.expunge(row)
            return ProviderManagementSnapshot(
                connections=tuple(connections),
                account_counts=account_counts,
                model_routes=tuple(model_routes),
                received_shares=tuple(received_shares),
            )

    # ------------------------------------------------------------------
    # Account-owner lifecycle
    # ------------------------------------------------------------------

    def owner_inventory(self, owner: str) -> dict[str, Any]:
        """Return a read-only fingerprint of every normalized owner reference."""

        owner = _owner(owner)
        with self._transaction() as db:
            connection_ids = sorted(
                value
                for value, in db.query(ProviderConnection.id).filter(
                    ProviderConnection.owner == owner
                ).all()
            )
            account_ids = sorted(
                value
                for value, in db.query(ProviderAccount.id).filter(
                    ProviderAccount.owner == owner
                ).all()
            )
            route_ids = sorted(
                value
                for value, in db.query(ProviderModelRoute.id).filter(
                    ProviderModelRoute.owner == owner
                ).all()
            )
            grant_ids = sorted(
                value
                for value, in db.query(ProviderShareGrant.id).filter(
                    or_(
                        ProviderShareGrant.owner == owner,
                        ProviderShareGrant.recipient == owner,
                    )
                ).all()
            )
            counts = {
                "connections": len(connection_ids),
                "accounts": len(account_ids),
                "model_routes": len(route_ids),
                "operation_bindings": db.query(ProviderOperationBinding).filter(
                    or_(
                        ProviderOperationBinding.owner == owner,
                        ProviderOperationBinding.credential_owner == owner,
                    )
                ).count(),
                "rotation_cursors": db.query(ProviderRotationCursor).filter(
                    ProviderRotationCursor.owner == owner
                ).count(),
                "route_bindings": db.query(ProviderRouteBinding).filter(
                    ProviderRouteBinding.owner == owner
                ).count(),
                "share_grants": len(grant_ids),
                "idempotency_records": db.query(ProviderIdempotencyRecord).filter(
                    ProviderIdempotencyRecord.owner == owner
                ).count(),
                "legacy_aliases": db.query(ProviderLegacyAlias).filter(
                    or_(
                        ProviderLegacyAlias.owner == owner,
                        ProviderLegacyAlias.connection_id.in_(connection_ids)
                        if connection_ids
                        else False,
                        ProviderLegacyAlias.model_route_id.in_(route_ids)
                        if route_ids
                        else False,
                        ProviderLegacyAlias.share_grant_id.in_(grant_ids)
                        if grant_ids
                        else False,
                    )
                ).count(),
                "credential_leases": db.query(ProviderCredentialLease).filter(
                    or_(
                        ProviderCredentialLease.owner == owner,
                        ProviderCredentialLease.credential_owner == owner,
                        ProviderCredentialLease.account_id.in_(account_ids)
                        if account_ids
                        else False,
                    )
                ).count(),
                "refresh_leases": (
                    db.query(ProviderRefreshLease).filter(
                        ProviderRefreshLease.account_id.in_(account_ids)
                    ).count()
                    if account_ids
                    else 0
                ),
                "account_health": (
                    db.query(ProviderAccountHealth).filter(
                        ProviderAccountHealth.account_id.in_(account_ids)
                    ).count()
                    if account_ids
                    else 0
                ),
                "account_model_health": (
                    db.query(ProviderAccountModelHealth).filter(
                        ProviderAccountModelHealth.account_id.in_(account_ids)
                    ).count()
                    if account_ids
                    else 0
                ),
                "entitlements": (
                    db.query(ProviderAccountEntitlement).filter(
                        or_(
                            ProviderAccountEntitlement.account_id.in_(account_ids)
                            if account_ids
                            else False,
                            ProviderAccountEntitlement.model_route_id.in_(route_ids)
                            if route_ids
                            else False,
                        )
                    ).count()
                    if account_ids or route_ids
                    else 0
                ),
            }
        material = {
            "counts": counts,
            "connection_ids": connection_ids,
            "account_ids": account_ids,
            "route_ids": route_ids,
            "grant_ids": grant_ids,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                material,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return {
            "count": int(sum(counts.values())),
            "counts": {key: int(value) for key, value in counts.items()},
            "fingerprint": fingerprint,
        }

    def rename_owner(self, old_owner: str, new_owner: str) -> dict[str, int]:
        """Atomically migrate every normalized-provider identity reference.

        Provider credentials cannot be migrated with a generic owner-column
        update: both their AEAD associated data and duplicate-detection digest
        are bound to the owner.  Open every source envelope before making any
        writes, then reseal and re-fingerprint it for the destination inside
        the same database transaction.

        Short-lived credential/refresh leases are invalidated instead of
        renamed.  Public-mutation idempotency digests are also owner-keyed and
        cannot be recomputed without the original client key, so those replay
        records are deliberately invalidated at the identity boundary.
        """

        old_owner = _owner(old_owner)
        new_owner = _owner(new_owner)
        if old_owner == new_owner:
            return {}

        counts: dict[str, int] = {}
        with self._transaction() as db:
            # Never merge credential-bearing or operational state into an
            # orphaned destination identity.  A recipient-only share may
            # legitimately have been provisioned before that username exists.
            destination_scopes = (
                (ProviderConnection, ProviderConnection.owner),
                (ProviderAccount, ProviderAccount.owner),
                (ProviderModelRoute, ProviderModelRoute.owner),
                (ProviderOperationBinding, ProviderOperationBinding.owner),
                (
                    ProviderOperationBinding,
                    ProviderOperationBinding.credential_owner,
                ),
                (ProviderRotationCursor, ProviderRotationCursor.owner),
                (ProviderRouteBinding, ProviderRouteBinding.owner),
                (ProviderShareGrant, ProviderShareGrant.owner),
                (ProviderIdempotencyRecord, ProviderIdempotencyRecord.owner),
                (ProviderLegacyAlias, ProviderLegacyAlias.owner),
            )
            for model, column in destination_scopes:
                if db.query(model).filter(column == new_owner).first() is not None:
                    raise ProviderConflict(
                        "target provider owner already has durable state"
                    )

            # An outgoing grant to the destination would become a self-share
            # after rename.  Reject it instead of silently changing authority.
            self_share = db.query(ProviderShareGrant).filter(
                ProviderShareGrant.owner == old_owner,
                ProviderShareGrant.recipient == new_owner,
            ).first()
            if self_share is not None:
                raise ProviderConflict(
                    "target provider owner is the recipient of a source share"
                )

            accounts = db.query(ProviderAccount).filter(
                ProviderAccount.owner == old_owner,
            ).all()
            resealed: dict[str, tuple[str, str]] = {}
            for account in accounts:
                if not account.credential_envelope:
                    continue
                credentials = unseal_credential(
                    account.credential_envelope,
                    CredentialScope(
                        old_owner,
                        account.connection_id,
                        account.id,
                        account.credential_version,
                    ),
                )
                resealed[account.id] = (
                    seal_credential(
                        credentials,
                        CredentialScope(
                            new_owner,
                            account.connection_id,
                            account.id,
                            account.credential_version,
                        ),
                    ),
                    credential_fingerprint(
                        credentials,
                        owner=new_owner,
                        connection_id=account.connection_id,
                    ),
                )

            account_ids = [account.id for account in accounts]
            if account_ids:
                counts["refresh_leases_invalidated"] = (
                    db.query(ProviderRefreshLease)
                    .filter(ProviderRefreshLease.account_id.in_(account_ids))
                    .delete(synchronize_session=False)
                )
            else:
                counts["refresh_leases_invalidated"] = 0
            counts["credential_leases_invalidated"] = (
                db.query(ProviderCredentialLease)
                .filter(
                    or_(
                        ProviderCredentialLease.owner == old_owner,
                        ProviderCredentialLease.credential_owner == old_owner,
                    )
                )
                .delete(synchronize_session=False)
            )
            counts["idempotency_records_invalidated"] = (
                db.query(ProviderIdempotencyRecord)
                .filter(ProviderIdempotencyRecord.owner == old_owner)
                .delete(synchronize_session=False)
            )

            def migrate(model: Any, column: Any, label: str) -> None:
                counts[label] = (
                    db.query(model)
                    .filter(column == old_owner)
                    .update({column: new_owner}, synchronize_session=False)
                )

            migrate(ProviderConnection, ProviderConnection.owner, "connections")
            migrate(ProviderModelRoute, ProviderModelRoute.owner, "model_routes")
            migrate(
                ProviderOperationBinding,
                ProviderOperationBinding.owner,
                "operation_bindings",
            )
            migrate(
                ProviderOperationBinding,
                ProviderOperationBinding.credential_owner,
                "shared_operation_bindings",
            )
            migrate(ProviderRotationCursor, ProviderRotationCursor.owner, "rotation_cursors")
            migrate(ProviderRouteBinding, ProviderRouteBinding.owner, "route_bindings")
            migrate(ProviderShareGrant, ProviderShareGrant.owner, "outgoing_shares")
            migrate(ProviderShareGrant, ProviderShareGrant.recipient, "incoming_shares")
            migrate(ProviderLegacyAlias, ProviderLegacyAlias.owner, "legacy_aliases")

            for account in accounts:
                account.owner = new_owner
                replacement = resealed.get(account.id)
                if replacement is not None:
                    account.credential_envelope, account.credential_fingerprint = replacement
            counts["accounts"] = len(accounts)

        return {key: int(value) for key, value in counts.items()}

    def purge_owner(self, owner: str) -> dict[str, int]:
        """Atomically remove one owner's complete normalized-provider state.

        This includes state where the account is the recipient or credential
        source, preventing a deleted-and-recreated username from inheriting
        old shares, sticky bindings, leases, aliases, or tombstones.
        """

        owner = _owner(owner)
        counts: dict[str, int] = {}
        with self._transaction() as db:
            connection_ids = [
                value
                for value, in db.query(ProviderConnection.id).filter(
                    ProviderConnection.owner == owner,
                ).all()
            ]
            account_ids = [
                value
                for value, in db.query(ProviderAccount.id).filter(
                    ProviderAccount.owner == owner,
                ).all()
            ]
            route_ids = [
                value
                for value, in db.query(ProviderModelRoute.id).filter(
                    ProviderModelRoute.owner == owner,
                ).all()
            ]
            grant_ids = [
                value
                for value, in db.query(ProviderShareGrant.id).filter(
                    or_(
                        ProviderShareGrant.owner == owner,
                        ProviderShareGrant.recipient == owner,
                    )
                ).all()
            ]

            def delete(query: Any, label: str) -> None:
                counts[label] = query.delete(synchronize_session=False)

            delete(
                db.query(ProviderCredentialLease).filter(
                    or_(
                        ProviderCredentialLease.owner == owner,
                        ProviderCredentialLease.credential_owner == owner,
                        ProviderCredentialLease.account_id.in_(account_ids)
                        if account_ids
                        else False,
                    )
                ),
                "credential_leases",
            )
            if account_ids:
                delete(
                    db.query(ProviderRefreshLease).filter(
                        ProviderRefreshLease.account_id.in_(account_ids)
                    ),
                    "refresh_leases",
                )
                delete(
                    db.query(ProviderAccountModelHealth).filter(
                        ProviderAccountModelHealth.account_id.in_(account_ids)
                    ),
                    "account_model_health",
                )
                delete(
                    db.query(ProviderAccountHealth).filter(
                        ProviderAccountHealth.account_id.in_(account_ids)
                    ),
                    "account_health",
                )
            if account_ids or route_ids:
                entitlement_filters = []
                if account_ids:
                    entitlement_filters.append(
                        ProviderAccountEntitlement.account_id.in_(account_ids)
                    )
                if route_ids:
                    entitlement_filters.append(
                        ProviderAccountEntitlement.model_route_id.in_(route_ids)
                    )
                delete(
                    db.query(ProviderAccountEntitlement).filter(
                        or_(*entitlement_filters)
                    ),
                    "entitlements",
                )

            delete(
                db.query(ProviderOperationBinding).filter(
                    or_(
                        ProviderOperationBinding.owner == owner,
                        ProviderOperationBinding.credential_owner == owner,
                        ProviderOperationBinding.connection_id.in_(connection_ids)
                        if connection_ids
                        else False,
                    )
                ),
                "operation_bindings",
            )
            delete(
                db.query(ProviderRotationCursor).filter(
                    or_(
                        ProviderRotationCursor.owner == owner,
                        ProviderRotationCursor.connection_id.in_(connection_ids)
                        if connection_ids
                        else False,
                    )
                ),
                "rotation_cursors",
            )
            delete(
                db.query(ProviderRouteBinding).filter(
                    or_(
                        ProviderRouteBinding.owner == owner,
                        ProviderRouteBinding.model_route_id.in_(route_ids)
                        if route_ids
                        else False,
                    )
                ),
                "route_bindings",
            )
            delete(
                db.query(ProviderLegacyAlias).filter(
                    or_(
                        ProviderLegacyAlias.owner == owner,
                        ProviderLegacyAlias.connection_id.in_(connection_ids)
                        if connection_ids
                        else False,
                        ProviderLegacyAlias.model_route_id.in_(route_ids)
                        if route_ids
                        else False,
                        ProviderLegacyAlias.share_grant_id.in_(grant_ids)
                        if grant_ids
                        else False,
                    )
                ),
                "legacy_aliases",
            )
            delete(
                db.query(ProviderShareGrant).filter(
                    or_(
                        ProviderShareGrant.owner == owner,
                        ProviderShareGrant.recipient == owner,
                        ProviderShareGrant.connection_id.in_(connection_ids)
                        if connection_ids
                        else False,
                    )
                ),
                "share_grants",
            )
            delete(
                db.query(ProviderIdempotencyRecord).filter(
                    ProviderIdempotencyRecord.owner == owner
                ),
                "idempotency_records",
            )
            delete(
                db.query(ProviderModelRoute).filter(ProviderModelRoute.owner == owner),
                "model_routes",
            )
            delete(
                db.query(ProviderAccount).filter(ProviderAccount.owner == owner),
                "accounts",
            )
            delete(
                db.query(ProviderConnection).filter(ProviderConnection.owner == owner),
                "connections",
            )

        return {key: int(value) for key, value in counts.items()}

    # ------------------------------------------------------------------
    # Public mutation idempotency
    # ------------------------------------------------------------------

    @staticmethod
    def idempotency_request_digest(
        *,
        owner: str,
        operation: str,
        payload: Mapping[str, Any],
    ) -> str:
        owner = _owner(owner)
        operation = _required(operation, "operation")
        try:
            canonical = json.dumps(
                dict(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ProviderValidationError(
                "idempotent request body is not JSON-compatible"
            ) from exc
        return keyed_digest(
            canonical,
            context=f"provider-api-request:{owner}:{operation}",
        )

    @staticmethod
    def deterministic_resource_id(
        *,
        prefix: str,
        owner: str,
        operation: str,
        idempotency_key: str,
    ) -> str:
        prefix = _required(prefix, "resource prefix")
        owner = _owner(owner)
        operation = _required(operation, "operation")
        key = _required(idempotency_key, "idempotency key")
        digest = keyed_digest(
            key,
            context=f"provider-api-resource:{owner}:{operation}",
        )
        return f"{prefix}_{digest[:32]}"

    @staticmethod
    def _idempotency_key_digest(
        *,
        owner: str,
        operation: str,
        idempotency_key: str,
    ) -> str:
        return keyed_digest(
            _required(idempotency_key, "idempotency key"),
            context=f"provider-api-idempotency:{owner}:{operation}",
        )

    @staticmethod
    def _validate_public_response(value: Any) -> None:
        forbidden = {
            "key",
            "api_key",
            "credential",
            "credentials",
            "credential_envelope",
            "credential_fingerprint",
            "token",
            "access_token",
            "refresh_token",
        }
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).strip().lower() in forbidden:
                    raise ProviderValidationError(
                        "idempotency response contains a secret-bearing field"
                    )
                ProviderStore._validate_public_response(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                ProviderStore._validate_public_response(item)

    def lookup_idempotency(
        self,
        *,
        owner: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
    ) -> Optional[IdempotencyResult]:
        owner = _owner(owner)
        operation = _required(operation, "operation")
        request_digest = _required(request_digest, "request digest")
        key_digest = self._idempotency_key_digest(
            owner=owner,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        with self._transaction() as db:
            row = db.query(ProviderIdempotencyRecord).filter(
                ProviderIdempotencyRecord.owner == owner,
                ProviderIdempotencyRecord.operation == operation,
                ProviderIdempotencyRecord.key_digest == key_digest,
            ).first()
            if row is None:
                return None
            if not hmac.compare_digest(row.request_digest, request_digest):
                raise ProviderConflict(
                    "idempotency key was already used for a different request"
                )
            return IdempotencyResult(
                status_code=row.status_code,
                response_body=dict(row.response_body or {}),
                resource_id=row.resource_id,
            )

    def record_idempotency(
        self,
        *,
        owner: str,
        operation: str,
        idempotency_key: str,
        request_digest: str,
        status_code: int,
        response_body: Mapping[str, Any],
        resource_id: Optional[str] = None,
    ) -> IdempotencyResult:
        owner = _owner(owner)
        operation = _required(operation, "operation")
        request_digest = _required(request_digest, "request digest")
        body = _json_mapping(response_body)
        self._validate_public_response(body)
        key_digest = self._idempotency_key_digest(
            owner=owner,
            operation=operation,
            idempotency_key=idempotency_key,
        )
        with self._transaction() as db:
            row = db.query(ProviderIdempotencyRecord).filter(
                ProviderIdempotencyRecord.owner == owner,
                ProviderIdempotencyRecord.operation == operation,
                ProviderIdempotencyRecord.key_digest == key_digest,
            ).first()
            if row is not None:
                if not hmac.compare_digest(row.request_digest, request_digest):
                    raise ProviderConflict(
                        "idempotency key was already used for a different request"
                    )
                return IdempotencyResult(
                    status_code=row.status_code,
                    response_body=dict(row.response_body or {}),
                    resource_id=row.resource_id,
                )
            row = ProviderIdempotencyRecord(
                id=f"pir_{uuid.uuid4().hex}",
                owner=owner,
                operation=operation,
                key_digest=key_digest,
                request_digest=request_digest,
                status_code=int(status_code),
                response_body=body,
                resource_id=(str(resource_id).strip() if resource_id else None),
            )
            db.add(row)
            db.flush()
            return IdempotencyResult(
                status_code=row.status_code,
                response_body=dict(row.response_body),
                resource_id=row.resource_id,
            )

    # ------------------------------------------------------------------
    # Managed-provider v1 direct dict transport
    # ------------------------------------------------------------------

    def _wire_account(self, *, owner: str, account: ProviderAccount) -> dict[str, Any]:
        access = self.credential_access(owner=owner, account_id=account.id)
        identity = {
            str(key): str(value)
            for key, value in dict(account.safe_identity or {}).items()
            if value is not None
        }
        payload: dict[str, Any] = {
            "id": account.id,
            "label": account.label,
            "enabled": bool(account.enabled),
            "order": account.sort_order,
            "revision": account.revision,
            "credentialRevision": account.credential_version,
            "credential": dict(access.credentials),
        }
        if identity:
            payload["identity"] = identity
        return payload

    @staticmethod
    def _optional_param(params: Mapping[str, Any], name: str) -> Optional[str]:
        value = params.get(name)
        return _required(value, name) if value is not None else None

    def managed_account_bind(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = self.bind_account(
            owner=owner,
            root_operation_id=_required(
                params.get("rootOperationID"),
                "rootOperationID",
            ),
            connection_id=_required(params.get("connectionID"), "connectionID"),
            provider_id=_required(params.get("providerID"), "providerID"),
            billing_lane=_required(params.get("billingLane"), "billingLane"),
            model_id=_required(params.get("modelID"), "modelID"),
            preferred_account_id=self._optional_param(
                params,
                "preferredAccountID",
            ),
            inherited_account_id=self._optional_param(
                params,
                "inheritedAccountID",
            ),
            grant_id=self._optional_param(params, "grantID"),
        )
        return selection.to_wire()

    def managed_account_commit(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = self.commit_account_binding(
            owner=owner,
            binding_id=_required(params.get("bindingID"), "bindingID"),
            expected_revision=int(params.get("expectedRevision")),
        )
        return selection.to_wire()

    def managed_account_attempt(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        selection = self.record_account_attempt(
            owner=owner,
            binding_id=_required(params.get("bindingID"), "bindingID"),
            expected_revision=int(params.get("expectedRevision")),
            account_id=self._optional_param(params, "accountID"),
            outcome=_required(params.get("outcome"), "outcome"),
            retry_after_ms=(
                int(params["retryAfterMs"])
                if params.get("retryAfterMs") is not None
                else None
            ),
            model_eligible=(
                bool(params["modelEligible"])
                if params.get("modelEligible") is not None
                else None
            ),
        )
        return selection.to_wire()

    def managed_credential_lease(
        self,
        *,
        owner: str,
        holder_id: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = self.lease_credential(
            owner=owner,
            holder_id=holder_id,
            root_operation_id=_required(
                params.get("rootOperationID"),
                "rootOperationID",
            ),
            connection_id=_required(params.get("connectionID"), "connectionID"),
            account_id=_required(params.get("accountID"), "accountID"),
            model_id=_required(params.get("modelID"), "modelID"),
            grant_id=self._optional_param(params, "grantID"),
            expected_credential_revision=int(
                params.get("expectedCredentialRevision")
            ),
        )
        return lease.to_wire()

    def managed_credential_replace(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection_id = _required(params.get("connectionID"), "connectionID")
        account_id = _required(params.get("accountID"), "accountID")
        current = self.get_account(owner=owner, account_id=account_id)
        if current.connection_id != connection_id:
            raise ProviderValidationError(
                "credential account belongs to a different connection"
            )
        account = self.replace_account_credential(
            owner=owner,
            account_id=account_id,
            expected_credential_version=int(params.get("expectedRevision")),
            credentials=_json_mapping(params.get("credential")),
        )
        return self._wire_account(owner=owner, account=account)

    @staticmethod
    def _ttl_seconds(params: Mapping[str, Any]) -> float:
        ttl_ms = params.get("ttlMs", 60000)
        try:
            ttl_ms = float(ttl_ms)
        except (TypeError, ValueError) as exc:
            raise ProviderValidationError("ttlMs must be a number") from exc
        if ttl_ms < 1 or ttl_ms > 60000:
            raise ProviderValidationError("ttlMs must be between 1 and 60000")
        return ttl_ms / 1000.0

    def managed_refresh_acquire(
        self,
        *,
        owner: str,
        holder_id: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = self.acquire_refresh_lease(
            owner=owner,
            holder_id=holder_id,
            connection_id=_required(params.get("connectionID"), "connectionID"),
            account_id=_required(params.get("accountID"), "accountID"),
            expected_credential_revision=int(params.get("expectedRevision")),
            ttl_seconds=self._ttl_seconds(params),
        )
        return lease.to_wire()

    def managed_refresh_renew(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        lease = self.renew_refresh_lease(
            owner=owner,
            token=_required(params.get("leaseID"), "leaseID"),
            ttl_seconds=self._ttl_seconds(params),
        )
        return lease.to_wire()

    def managed_refresh_commit(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection_id = _required(params.get("connectionID"), "connectionID")
        account_id = _required(params.get("accountID"), "accountID")
        current = self.get_account(owner=owner, account_id=account_id)
        if current.connection_id != connection_id:
            raise ProviderValidationError(
                "refresh account belongs to a different connection"
            )
        account = self.commit_refresh(
            owner=owner,
            account_id=account_id,
            token=_required(params.get("leaseID"), "leaseID"),
            expected_credential_revision=int(params.get("expectedRevision")),
            credentials=_json_mapping(params.get("credential")),
        )
        return self._wire_account(owner=owner, account=account)

    def managed_refresh_abort(
        self,
        *,
        owner: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        self.abort_refresh(
            owner=owner,
            token=_required(params.get("leaseID"), "leaseID"),
        )
        return {}

    def handle_managed_method(
        self,
        *,
        owner: str,
        holder_id: str,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Dispatch one secret-bearing ACP extMethod using the v1 wire shapes."""

        if not isinstance(params, Mapping):
            raise ProviderValidationError("managed provider params must be an object")
        handlers = {
            "_openclank/provider-store/v1/account/bind": lambda: self.managed_account_bind(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/account/commit": lambda: self.managed_account_commit(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/account/attempt": lambda: self.managed_account_attempt(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/credential/lease": lambda: self.managed_credential_lease(
                owner=owner,
                holder_id=holder_id,
                params=params,
            ),
            "_openclank/provider-store/v1/credential/replace": lambda: self.managed_credential_replace(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/refresh/acquire": lambda: self.managed_refresh_acquire(
                owner=owner,
                holder_id=holder_id,
                params=params,
            ),
            "_openclank/provider-store/v1/refresh/renew": lambda: self.managed_refresh_renew(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/refresh/commit": lambda: self.managed_refresh_commit(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/refresh/abort": lambda: self.managed_refresh_abort(
                owner=owner,
                params=params,
            ),
        }
        handler = handlers.get(_required(method, "managed method"))
        if handler is None:
            raise ProviderValidationError("unsupported managed provider method")
        return handler()


__all__ = [
    "SAFE_DISCLOSURE_FIELDS",
    "AccountSelector",
    "ModelSelector",
    "CredentialAccess",
    "AccountSelection",
    "CredentialLeaseGrant",
    "RefreshLeaseGrant",
    "ShareScope",
    "ReceivedShareProjection",
    "ProviderManagementSnapshot",
    "IdempotencyResult",
    "ProviderStore",
    "ProviderStoreError",
    "ProviderValidationError",
    "ProviderNotFound",
    "ProviderConflict",
    "ProviderRevisionConflict",
    "BillingLaneMismatch",
    "NoEligibleAccount",
    "RefreshLeaseBusy",
    "RefreshLeaseInvalid",
    "ShareDenied",
]

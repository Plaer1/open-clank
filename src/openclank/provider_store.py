"""Owner-scoped repository and selection rules for normalized providers.

The repository is the durable Open Clank boundary used by the managed MiMo
protocol.  It deliberately knows nothing about individual provider token
formats: credentials are opaque JSON objects sealed with authenticated tenant
context, while connection/account semantics are validated by the engine.
"""

from __future__ import annotations

import math
import copy

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import base64
from functools import wraps
import hmac
import json
import re
import secrets
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence
import uuid

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError, OperationalError

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
_MAX_POLICY_SNAPSHOT_ACCOUNTS = 1024


def _account_pool_policy(value: Any) -> Optional[dict[str, Any]]:
    """Validate and normalize the optional explicit account-pool policy."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProviderValidationError("account_pool_policy must be an object or null")
    if set(value) - {"version", "mode", "account_id", "same_model_fallback"}:
        raise ProviderValidationError("account_pool_policy contains unsupported fields")
    if type(value.get("version")) is not int or value["version"] != 1:
        raise ProviderValidationError("account_pool_policy version is unsupported")
    mode = value.get("mode")
    if mode not in {"round_robin", "preferred", "pinned"}:
        raise ProviderValidationError("account_pool_policy mode is unsupported")
    fallback = value.get("same_model_fallback")
    if type(fallback) is not bool:
        raise ProviderValidationError("same_model_fallback must be a boolean")
    account_id = value.get("account_id")
    if mode == "round_robin":
        if account_id is not None:
            raise ProviderValidationError("round_robin policy cannot name an account")
    else:
        account_id = _required(account_id, "account_id")
        if len(account_id) > 256:
            raise ProviderValidationError("account_id is too long")
    if mode == "pinned" and fallback:
        raise ProviderValidationError("pinned policy cannot enable fallback")
    return {
        "version": 1,
        "mode": mode,
        **({"account_id": account_id} if account_id is not None else {}),
        "same_model_fallback": fallback,
    }


def _account_pool_policy_snapshot(
    *,
    connection: ProviderConnection,
    policy: Optional[Mapping[str, Any]],
    candidates: Sequence[ProviderAccount],
) -> dict[str, Any]:
    if len(candidates) > _MAX_POLICY_SNAPSHOT_ACCOUNTS:
        raise ProviderValidationError("provider account pool exceeds the policy snapshot limit")
    return {
        "version": 1,
        "source": "explicit" if policy is not None else "legacy",
        "policy_revision": int(connection.revision),
        "mode": str(policy["mode"]) if policy is not None else "round_robin",
        **({"account_id": str(policy["account_id"])} if policy is not None and policy.get("account_id") else {}),
        "same_model_fallback": bool(policy["same_model_fallback"]) if policy is not None else True,
        "ordered_account_ids": [account.id for account in candidates],
    }


def _erase_optional_quota_rows(db, *, owner: str, account_ids: Sequence[str]) -> None:
    """Erase quota metadata when the optional Stats schema is installed.

    Provider lifecycle operations are also used by the early provider-only
    schema and by staged migrations.  Missing optional tables must not make a
    credential tombstone fail, while unrelated SQL errors remain fatal.
    """
    if not account_ids:
        return
    from core.stats_models import StatsQuotaObservation, StatsQuotaCycle

    try:
        # Keep a missing optional table from aborting the enclosing provider
        # lifecycle transaction on databases that mark the outer transaction
        # failed after an OperationalError.
        with db.begin_nested():
            db.query(StatsQuotaObservation).filter(
                StatsQuotaObservation.owner == owner,
                StatsQuotaObservation.account_id.in_(account_ids),
            ).delete(synchronize_session=False)
            db.query(StatsQuotaCycle).filter(
                StatsQuotaCycle.owner == owner,
                StatsQuotaCycle.account_id.in_(account_ids),
            ).delete(synchronize_session=False)
    except OperationalError as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if "no such table" not in message or not any(
            table in message
            for table in ("stats_quota_observations", "stats_quota_cycles")
        ):
            raise


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


def _provider_acquisition_method(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = _required(value, "acquisition_method")
    if normalized not in {"manual-key", "browser", "device"}:
        raise ProviderValidationError("unsupported provider acquisition method")
    return normalized


def _owner(value: Any) -> str:
    return _required(value, "owner").lower()


def _actor_execution_scope(
    *,
    session_id: Optional[str],
    actor_id: Optional[str],
    execution_revision: Optional[int],
    model_route_id: Optional[str],
) -> tuple[str, Optional[str], Optional[str], Optional[int], Optional[str]]:
    """Return the immutable provider-binding scope while retaining legacy rows.

    The digest is a database consistency key, not an authorization credential.
    Root, route, grant, account, and current credential revision are validated
    independently and remain stored as ordinary binding fields.
    """
    actor_identity = (session_id, actor_id, execution_revision)
    normalized_route = _required(model_route_id, "model_route_id") if model_route_id is not None else None
    if all(value is None for value in actor_identity):
        return "legacy", None, None, None, normalized_route
    if any(value is None for value in actor_identity):
        raise ProviderValidationError("actor execution binding identity is incomplete")
    normalized_session = _required(session_id, "session_id")
    normalized_actor = _required(actor_id, "actor_id")
    if normalized_route is None:
        raise ProviderValidationError("actor execution binding requires model_route_id")
    if len(normalized_session) > 256 or len(normalized_actor) > 200:
        raise ProviderValidationError("actor execution identity is too long")
    if (
        isinstance(execution_revision, bool)
        or not isinstance(execution_revision, int)
        or execution_revision < 0
        or execution_revision > (2**53 - 1)
    ):
        raise ProviderValidationError("execution_revision must be a nonnegative safe integer")
    encoded = json.dumps(
        [normalized_session, normalized_actor, execution_revision],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    scope_key = "actor-v1:" + hashlib.sha256(encoded).hexdigest()
    return scope_key, normalized_session, normalized_actor, execution_revision, normalized_route


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


def _merge_route_provenance(
    route: Mapping[str, Any], discovery: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Keep route producer authority distinct from account discovery metadata."""

    result = _json_mapping(route)
    for key, value in _json_mapping(discovery).items():
        if key not in {"authority", "origin", "catalog", "familyID", "metadataCatalog"}:
            result[key] = value
    return result


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


def _preferences_before_provider(function):
    @wraps(function)
    def locked(*args, **kwargs):
        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK:
            return function(*args, **kwargs)
    return locked


_METHOD_POLICY_SNAPSHOT_KEY = "openclank.provider_method_policy.v1"
_LOGGING_POLICY_SNAPSHOT_KEY = "openclank.logging_policy.v1"


def _validate_logging_policy_snapshot(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    fields = {'version','revision','advanced_enabled','request_body_enabled','response_body_enabled','binary_body_enabled','private'}
    if (type(value) is not dict or set(value) != fields or type(value['version']) is not int or value['version'] != 1
        or type(value['revision']) is not int or value['revision'] < 1
        or any(type(value[key]) is not bool for key in fields - {'version','revision'})):
        raise ProviderValidationError('logging policy snapshot unavailable')
    return dict(value)

def _check_method_policy(proof: Any, credential_owner: str, account: Any = None) -> dict[str, Any]:
    from src.openclank.provider_method_policy import require_enabled, ProviderMethodPolicyError
    if (not isinstance(proof, dict) or set(proof) != {"version", "methodID", "credentialOwner", "policyRevision"}
        or type(proof["version"]) is not int or proof["version"] != 1
        or proof["methodID"] != "anthropic.claude_subscription"
        or proof["credentialOwner"] != credential_owner
        or type(proof["policyRevision"]) is not int or proof["policyRevision"] < 1):
        raise ProviderValidationError("Claude subscription policy proof is invalid")
    if account is not None and account.auth_class != "subscription":
        raise ProviderValidationError("Claude subscription account has incompatible auth class")
    try:
        require_enabled(credential_owner, proof["policyRevision"])
    except ProviderMethodPolicyError as exc:
        raise ProviderValidationError("Claude subscription policy is disabled or stale") from exc
    return dict(proof)


def _binding_method_policy(binding: Any) -> Any:
    snapshot = binding.policy_snapshot
    return snapshot.get(_METHOD_POLICY_SNAPSHOT_KEY) if isinstance(snapshot, Mapping) else None


def _validated_dispatch_observation(value: Any, outcome: str, evidence: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    keys={'version','profile','captureDispatchID','upstreamDispatchID','httpStatus'}
    if (type(value) is not dict or set(value)!=keys or type(value['version']) is not int or value['version']!=1
        or value['profile'] not in ('anthropic.messages.v1','openai.codex.responses.v1')
        or any(type(value[k]) is not str or re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',value[k]) is None
               for k in ('captureDispatchID','upstreamDispatchID'))
        or type(value['httpStatus']) is not int
        or (outcome in {'success','invalid_output'} and not 200<=value['httpStatus']<=299)
        or (outcome not in {'success','invalid_output'} and (not 400<=value['httpStatus']<=599 or type(evidence) is not dict or evidence.get('httpStatus')!=value['httpStatus'] or evidence.get('kind')=='malformed_success'))
        or ((value['captureDispatchID']==value['upstreamDispatchID']) != (value['profile']=='anthropic.messages.v1'))):
        raise ProviderValidationError('provider dispatch observation is invalid')
    return dict(value)


def _validated_attempt_evidence(value: Any, outcome: str) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict) or not {"version", "kind"} <= set(value) or set(value) - {"version", "kind", "httpStatus", "providerReset", "codexWindows", "localNotBefore"}:
        raise ProviderValidationError("provider attempt evidence is invalid")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ProviderValidationError("provider attempt evidence version is invalid")
    mapping = {"throttle":"transient", "exhausted":"quota", "auth":"auth", "entitlement":"entitlement", "transient":"transient", "malformed_success":"unknown", "unknown":"unknown"}
    if mapping.get(value.get("kind")) != outcome:
        raise ProviderValidationError("provider attempt evidence disagrees with outcome")
    status = value.get("httpStatus")
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise ProviderValidationError("provider attempt status is invalid")
    if "providerReset" in value:
        reset = value['providerReset']
        if (type(reset) is not dict or set(reset) not in ({'at','source','observedAt'}, {'at','source','observedAt','dispatchID'})
            or ('dispatchID' in reset and (type(reset['dispatchID']) is not str or not 1 <= len(reset['dispatchID']) <= 128))
            or reset['source'] != 'anthropic.requests.rate-limit.v1'
            or value['kind'] != 'throttle' or status != 429):
            raise ProviderValidationError('provider request-rate evidence is invalid')
        parsed = []
        for key in ('at','observedAt'):
            raw = reset[key]
            if type(raw) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z',raw) is None:
                raise ProviderValidationError('provider request-rate timestamp is invalid')
            try:
                parsed.append(datetime.fromisoformat(raw[:-1] + '+00:00'))
            except ValueError as exc:
                raise ProviderValidationError('provider request-rate timestamp is invalid') from exc
        if parsed[0] < parsed[1] or parsed[0] - parsed[1] > timedelta(days=31):
            raise ProviderValidationError('provider request-rate timestamp is invalid')
    if 'codexWindows' in value:
        evidence = value['codexWindows']
        if (type(evidence) is not dict or set(evidence) != {'source','observedAt','dispatchID','windows'}
            or evidence['source'] != 'openai.codex.subscription.headers.v1' or value['kind'] != 'throttle' or status != 429
            or type(evidence['dispatchID']) is not str or not 1 <= len(evidence['dispatchID']) <= 128
            or type(evidence['windows']) is not list or not 1 <= len(evidence['windows']) <= 2):
            raise ProviderValidationError('Codex window evidence is invalid')
        def timestamp(raw):
            if type(raw) is not str or re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z',raw) is None:
                raise ProviderValidationError('Codex window timestamp is invalid')
            try: return datetime.fromisoformat(raw[:-1] + '+00:00')
            except ValueError as exc: raise ProviderValidationError('Codex window timestamp is invalid') from exc
        observed = timestamp(evidence['observedAt'])
        names = []
        for window in evidence['windows']:
            if (type(window) is not dict or not {'window','usedPercent'} <= set(window) or set(window) - {'window','usedPercent','windowMinutes','resetAt'}
                or window['window'] not in ('primary','secondary') or type(window['usedPercent']) not in (int,float)
                or not math.isfinite(window['usedPercent']) or not 0 <= window['usedPercent'] <= 10000
                or ('windowMinutes' in window and (type(window['windowMinutes']) is not int or not 1 <= window['windowMinutes'] <= 525600))):
                raise ProviderValidationError('Codex window evidence is invalid')
            names.append(window['window'])
            if 'resetAt' in window:
                reset = timestamp(window['resetAt'])
                if reset < observed or reset - observed > timedelta(days=366): raise ProviderValidationError('Codex window timestamp is invalid')
        if len(set(names)) != len(names): raise ProviderValidationError('Codex window evidence is invalid')
    if "localNotBefore" in value:
        raw = value["localNotBefore"]
        if type(raw) is not str or len(raw) > 40 or not raw.endswith("Z"):
            raise ProviderValidationError("local retry time is invalid")
        try:
            datetime.fromisoformat(raw[:-1] + "+00:00")
        except ValueError as exc:
            raise ProviderValidationError("local retry time is invalid") from exc
    return dict(value)


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
    scope_kind: str = "auxiliary_operation"
    operation_id: Optional[str] = None
    session_id: Optional[str] = None
    actor_id: Optional[str] = None
    execution_revision: Optional[int] = None
    model_route_id: Optional[str] = None
    method_policy: Optional[Mapping[str, Any]] = None

    def to_wire(self) -> dict[str, Any]:
        """Managed-provider v1 ``accountBinding`` representation."""

        result = {
            "bindingID": self.binding_id,
            "bindingRevision": self.binding_revision,
            "rootOperationID": self.root_operation_id,
            "scopeKind": self.scope_kind,
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "modelID": self.model_id,
            "credentialRequired": self.account_id is not None,
            "source": self.source,
            "attempt": self.attempt,
            "committed": self.committed,
        }
        if self.method_policy is not None:
            result["methodPolicy"] = dict(self.method_policy)
        if self.scope_kind == "actor_execution":
            if self.session_id is not None:
                result["sessionID"] = self.session_id
            if self.actor_id is not None:
                result["actorID"] = self.actor_id
            if self.execution_revision is not None:
                result["executionRevision"] = self.execution_revision
        elif self.operation_id is not None:
            result["operationID"] = self.operation_id
        if self.model_route_id is not None:
            result["modelRouteID"] = self.model_route_id
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
    method_policy: Optional[Mapping[str, Any]] = None

    def to_wire(self) -> dict[str, Any]:
        return {
            **({"methodPolicy": dict(self.method_policy)} if self.method_policy is not None else {}),
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
    binding_id: Optional[str] = None
    session_id: Optional[str] = None
    actor_id: Optional[str] = None
    execution_revision: Optional[int] = None
    root_operation_id: Optional[str] = None
    model_route_id: Optional[str] = None
    scope_kind: str = "auxiliary_operation"
    operation_id: Optional[str] = None
    method_policy: Optional[Mapping[str, Any]] = None

    def to_wire(self) -> dict[str, Any]:
        result = {
            "leaseID": self.lease_id,
            "connectionID": self.connection_id,
            "accountID": self.account_id,
            "credentialRevision": self.credential_revision,
            "expiresAt": _epoch_milliseconds(self.expires_at),
            "credential": dict(self.credentials),
            "scopeKind": self.scope_kind,
        }
        if self.method_policy is not None:
            result["methodPolicy"] = dict(self.method_policy)
        if self.binding_id is not None:
            result["bindingID"] = self.binding_id
        if self.root_operation_id is not None:
            result["rootOperationID"] = self.root_operation_id
        if self.scope_kind == "actor_execution":
            if self.session_id is not None:
                result["sessionID"] = self.session_id
            if self.actor_id is not None:
                result["actorID"] = self.actor_id
            if self.execution_revision is not None:
                result["executionRevision"] = self.execution_revision
            if self.model_route_id is not None:
                result["modelRouteID"] = self.model_route_id
        elif self.operation_id is not None:
            result["operationID"] = self.operation_id
        return result


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
    reauth_counts: Mapping[str, int]
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
            connection = db.connection()
            if connection.dialect.name == 'sqlite':
                from src.openclank.provider_cross_model import profile_absent, profile_ready, set_writer
                set_writer(connection)
                if not profile_absent(connection) and not profile_ready(connection):
                    raise ProviderValidationError('cross-model profile unavailable')
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

    @contextmanager
    def _credential_transaction(self):
        """Serialize disclosure with authority writes on the supported store."""
        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK, self._transaction() as db:
            if db.get_bind().dialect.name != 'sqlite':
                raise ProviderValidationError('credential disclosure requires the validated SQLite store')
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
            yield db

    @contextmanager
    def _cross_model_transaction(self, admission, callback_authority, *, absent_ok=False):
        """Inactive host-only coordinator transaction under the F2 admission."""
        from src.openclank.acp_bridge import _ManagedProviderTransactionAdmission
        from src.openclank.provider_callbacks import ManagedProviderCallbacks
        from src.openclank.provider_cross_model import profile_ready, profile_absent
        from routes.prefs_routes import PREFERENCE_LOCK
        if (type(admission) is not _ManagedProviderTransactionAdmission
                or type(callback_authority) is not ManagedProviderCallbacks
                or admission._callback is not callback_authority or callback_authority._store is not self or callback_authority.owner != admission.owner
                or callback_authority.holder_id != admission.holder_id):
            raise ProviderValidationError('cross-model authority unavailable')
        admission.assert_current()
        with callback_authority._generation_lock, PREFERENCE_LOCK:
            if callback_authority._generation_closed.is_set():
                raise ProviderValidationError('cross-model authority unavailable')
            admission.assert_current()
            db = self._session_factory()
            connection = None
            original_timeout = None
            begun = False
            try:
                connection = db.connection()
                if connection.dialect.name != 'sqlite':
                    raise ProviderValidationError('cross-model profile unavailable')
                from src.openclank.provider_cross_model import set_writer
                set_writer(connection)
                original_timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
                connection.exec_driver_sql('PRAGMA busy_timeout=0')
                connection.exec_driver_sql('BEGIN IMMEDIATE')
                begun = True
                available = profile_ready(connection)
                if not available and not (absent_ok and profile_absent(connection)):
                    raise ProviderValidationError('cross-model profile unavailable')
                admission.assert_current()
                yield db, connection if available else None
                admission.assert_current()
                if callback_authority._generation_closed.is_set():
                    raise ProviderValidationError('cross-model authority unavailable')
                db.flush()
                connection.exec_driver_sql('COMMIT')
                begun = False
            except BaseException:
                if connection is not None and begun:
                    try: connection.exec_driver_sql('ROLLBACK')
                    except Exception: connection.invalidate()
                raise
            finally:
                if connection is not None:
                    try: set_writer(connection)
                    except Exception: connection.invalidate()
                if connection is not None and original_timeout is not None:
                    try:
                        connection.exec_driver_sql('PRAGMA busy_timeout=' + str(int(original_timeout)))
                    except Exception:
                        connection.invalidate()
                try: db.rollback()
                finally: db.close()

    @contextmanager
    def _fresh_binding_transaction(self, proof):
        from src.openclank.provider_callbacks import _FreshMainBinding, ManagedProviderCallbacks
        from src.openclank.provider_cross_model import profile_ready, profile_absent, set_writer
        from routes.prefs_routes import PREFERENCE_LOCK
        if type(proof) is not _FreshMainBinding or type(proof.callback) is not ManagedProviderCallbacks or proof.callback._store is not self:
            raise ProviderValidationError('fresh main authority unavailable')
        proof.assert_current()
        with proof.callback._generation_lock, PREFERENCE_LOCK:
            proof.assert_current()
            if proof.callback._generation_closed.is_set():
                raise ProviderValidationError('fresh main authority unavailable')
            db = self._session_factory(); connection = None; timeout = None; begun = False
            try:
                connection = db.connection()
                if connection.dialect.name != 'sqlite': raise ProviderValidationError('fresh main profile unavailable')
                set_writer(connection)
                timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
                connection.exec_driver_sql('PRAGMA busy_timeout=0')
                connection.exec_driver_sql('BEGIN IMMEDIATE'); begun = True
                if not profile_ready(connection) and not profile_absent(connection):
                    raise ProviderValidationError('fresh main profile unavailable')
                proof.assert_current()
                yield db
                proof.assert_current()
                if proof.callback._generation_closed.is_set(): raise ProviderValidationError('fresh main authority unavailable')
                db.flush(); connection.exec_driver_sql('COMMIT'); begun = False
            except BaseException:
                if connection is not None and begun:
                    try: connection.exec_driver_sql('ROLLBACK')
                    except Exception: connection.invalidate()
                raise
            finally:
                if connection is not None:
                    try: set_writer(connection)
                    except Exception: connection.invalidate()
                if connection is not None and timeout is not None:
                    try: connection.exec_driver_sql('PRAGMA busy_timeout=' + str(int(timeout)))
                    except Exception: connection.invalidate()
                try: db.rollback()
                finally: db.close()

    def _cross_model_alternatives(self, db, admission, primary, policy):
        from src.openclank.chat_routing import _shared_request_option_route_id
        from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
        result = []
        seen = {(primary.connection_id, primary.model_id)}
        for item in policy['alternatives']:
            route = db.query(ProviderModelRoute).filter_by(id=item['route_id'], enabled=True, deleted_at=None).first()
            if route is None or route.id == primary.model_route_id:
                raise ProviderValidationError('cross-model route unavailable')
            connection = self._require_connection(db, route.owner, route.connection_id)
            if (not connection.enabled or connection.billing_lane != primary.billing_lane
                    or not {'chat.stream', 'chat.complete'}.intersection(route.operations or ())
                    or (connection.id, route.provider_model_id) in seen):
                raise ProviderValidationError('cross-model route unavailable')
            seen.add((connection.id, route.provider_model_id))
            grant = None
            allowed = None
            if route.owner != admission.owner:
                grants = db.query(ProviderShareGrant).filter_by(owner=route.owner, recipient=admission.owner,
                    connection_id=connection.id, billing_lane=connection.billing_lane, state='active').limit(1025).all()
                if len(grants) > 1024:
                    raise ShareDenied('cross-model grant inventory unavailable')
                grants = [g for g in grants if ModelSelector.parse(g.model_selector).includes(route.id)]
                if len(grants) != 1:
                    raise ShareDenied('cross-model grant unavailable')
                grant = grants[0]
                selector = AccountSelector.parse(grant.account_selector)
                if selector.mode == 'explicit_accounts': allowed = set(selector.account_ids)
            option_id = _shared_request_option_route_id(admission.owner, grant.id, route.id) if grant else route.id
            descriptors = project_request_options(route.capabilities, catalog_revision=route.catalog_revision, source='engine-catalog')
            options = validate_request_option_selection(route_id=option_id, catalog_revision=route.catalog_revision,
                descriptors=descriptors, selection=item['options'])
            if options != item['options']['options']:
                raise ProviderValidationError('cross-model options unavailable')
            if len(db.query(ProviderAccount.id).filter_by(owner=route.owner, connection_id=connection.id).limit(1025).all()) > 1024:
                raise ProviderValidationError('cross-model account inventory unavailable')
            accounts = self._eligible_account_rows(db, owner=route.owner, connection=connection,
                model_route_id=route.id, allowed_account_ids=allowed, now=_utc(self._clock()))
            if not any(account.credential_envelope for account in accounts):
                raise NoEligibleAccount('cross-model account unavailable')
            if connection.family_id == 'anthropic' and connection.billing_lane == 'subscription':
                raise ProviderValidationError('cross-model Claude subscription unavailable')
            result.append({'route_id':route.id, 'route_revision':route.revision,
                'connection_id':connection.id, 'connection_revision':connection.revision,
                'provider_id':connection.family_id, 'model_id':route.provider_model_id,
                'lane':connection.billing_lane, 'grant_id':grant.id if grant else None,
                'grant_revision':grant.revision if grant else None, 'options':copy.deepcopy(item['options'])})
        return result

    @staticmethod
    def _cross_model_original(admission):
        return {'owner':admission.owner, 'account_id':admission.account_id, 'chat_id':admission.chat_id,
            'session_id':admission.session_id, 'root_id':admission.root_id, 'actor_id':admission.actor_id,
            'inner_turn_id':admission.inner_turn_id, 'execution_revision':admission.execution_revision,
            'holder_id':admission.holder_id, 'generation':admission.generation, 'owner_epoch':admission.owner_epoch,
            'root_nonce':getattr(admission._root, 'provider_root_nonce', None),
            'provider_id':admission.provider_id, 'model_id':admission.model_id,
            'options_sha256':hashlib.sha256(admission.option_context_json.encode('utf-8')).hexdigest()}

    @staticmethod
    def _cross_model_policy(connection, route_id):
        from src.openclank.provider_cross_model import validate_policy
        row = connection.exec_driver_sql('SELECT cross_model_policy FROM provider_model_routes WHERE id=?', (route_id,)).first()
        if row is None:
            raise ProviderNotFound('cross-model route unavailable')
        if row[0] is None:
            return None
        try:
            return validate_policy(json.loads(row[0]))
        except (ValueError, TypeError):
            raise ProviderValidationError('cross-model policy unavailable') from None

    def read_cross_model_policy(self, admission, callback_authority, route_id):
        with self._cross_model_transaction(admission, callback_authority, absent_ok=True) as (db, connection):
            if connection is None: return None
            route = self._require_route(db, admission.owner, _required(route_id, 'route_id'))
            if not route.enabled: raise ProviderValidationError('cross-model route unavailable')
            return self._cross_model_policy(connection, route.id)

    def set_cross_model_policy(self, admission, callback_authority, route_id, expected_route_revision, policy):
        if type(expected_route_revision) is not int or expected_route_revision < 1:
            raise ProviderValidationError('cross-model route revision unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            return self._set_cross_model_policy_tx(db, connection, admission, route_id, expected_route_revision, policy)

    def _set_cross_model_policy_tx(self, db, connection, admission, route_id, expected_route_revision, policy):
        from src.openclank.provider_cross_model import validate_policy
        from types import SimpleNamespace
        policy = validate_policy(policy)
        route = self._require_route(db, admission.owner, _required(route_id, 'route_id'))
        self._require_revision(route.revision, expected_route_revision)
        conn = self._require_connection(db, admission.owner, route.connection_id)
        if not route.enabled or not conn.enabled: raise ProviderValidationError('cross-model route unavailable')
        old = self._cross_model_policy(connection, route.id)
        if policy['revision'] != (old['revision'] if old else 0) + 1:
            raise ProviderConflict('cross-model policy revision changed')
        if policy['enabled']:
            self._cross_model_alternatives(db, admission, SimpleNamespace(connection_id=conn.id,
                model_id=route.provider_model_id, model_route_id=route.id, billing_lane=conn.billing_lane), policy)
        changed = connection.exec_driver_sql('UPDATE provider_model_routes SET cross_model_policy=?, revision=revision+1 WHERE id=? AND owner=? AND revision=?',
            (json.dumps(policy, sort_keys=True, separators=(',', ':')), route.id, admission.owner, expected_route_revision))
        if changed.rowcount != 1: raise ProviderConflict('cross-model route revision changed')
        return copy.deepcopy(policy)

    @contextmanager
    def _human_cross_model_transaction(self, admission, *, write):
        from src.openclank.provider_human_policy import HumanProviderPolicyAdmission
        from src.openclank.provider_cross_model import profile_ready, profile_absent, set_writer
        from routes.prefs_routes import PREFERENCE_LOCK
        if type(admission) is not HumanProviderPolicyAdmission or admission._store is not self:
            raise ProviderValidationError('human provider policy authority unavailable')
        admission.assert_current()
        with PREFERENCE_LOCK:
            admission.assert_current()
            db = self._session_factory(); connection = None; timeout = None; begun = False
            try:
                connection = db.connection()
                if connection.dialect.name != 'sqlite': raise ProviderValidationError('cross-model profile unavailable')
                set_writer(connection)
                timeout = connection.exec_driver_sql('PRAGMA busy_timeout').scalar_one()
                connection.exec_driver_sql('PRAGMA busy_timeout=0')
                connection.exec_driver_sql('BEGIN IMMEDIATE'); begun = True
                available = profile_ready(connection)
                if not available and (write or not profile_absent(connection)):
                    raise ProviderValidationError('cross-model profile unavailable')
                admission.assert_current()
                yield db, connection if available else None
                admission.assert_current()
                db.flush(); connection.exec_driver_sql('COMMIT'); begun = False
            except BaseException as error:
                if connection is not None and begun:
                    try: connection.exec_driver_sql('ROLLBACK')
                    except Exception: connection.invalidate()
                if isinstance(error, OperationalError):
                    code = getattr(error.orig, 'sqlite_errorcode', None)
                    if type(code) is int and (code & 255) in (5, 6):
                        raise ProviderConflict('provider policy store is busy') from None
                raise
            finally:
                if connection is not None:
                    try: set_writer(connection)
                    except Exception: connection.invalidate()
                if connection is not None and timeout is not None:
                    try: connection.exec_driver_sql('PRAGMA busy_timeout=' + str(int(timeout)))
                    except Exception: connection.invalidate()
                try: db.rollback()
                finally: db.close()

    def _human_cross_model_readiness(self, db, connection, admission, route, policy):
        """Informational copied-profile/route check, never a turn or credential grant."""
        if connection is None: return 'profile_unavailable'
        if policy is None or not policy.get('enabled'): return 'not_configured'
        try:
            if len(policy['alternatives']) != 1: return 'route_unavailable'
            primary = self._require_connection(db, admission.owner, route.connection_id)
            def official(value):
                return (value.enabled and value.deleted_at is None and value.family_id == 'anthropic'
                    and value.adapter_id == 'anthropic-messages' and value.kind == 'official'
                    and value.billing_lane == 'metered_api' and (value.normalized_url is None
                        or value.normalized_url.rstrip('/') in {'https://api.anthropic.com','https://api.anthropic.com/v1'}))
            if not route.enabled or 'chat.stream' not in (route.operations or ()) or not official(primary): return 'route_unavailable'
            from types import SimpleNamespace
            alternatives=self._cross_model_alternatives(db,admission,SimpleNamespace(connection_id=primary.id,
                model_id=route.provider_model_id,model_route_id=route.id,billing_lane=primary.billing_lane),policy)
            if len(alternatives)!=1 or alternatives[0]['grant_id'] is not None: return 'route_unavailable'
            alternative=self._require_route(db,admission.owner,alternatives[0]['route_id'])
            target=self._require_connection(db,admission.owner,alternative.connection_id)
            if not official(target) or 'chat.stream' not in (alternative.operations or ()): return 'route_unavailable'
            selected_ids=[]
            for item, provider in ((route,primary),(alternative,target)):
                if len(db.query(ProviderAccount.id).filter_by(owner=admission.owner,connection_id=provider.id).limit(1025).all())>1024: return 'route_unavailable'
                candidates=self._eligible_account_rows(db,owner=admission.owner,connection=provider,model_route_id=item.id,allowed_account_ids=None,now=_utc(self._clock()))
                if (len(candidates)!=1 or candidates[0].auth_method!='api_key' or candidates[0].auth_class!='metered'
                        or candidates[0].acquisition_method!='manual-key' or not candidates[0].credential_envelope): return 'route_unavailable'
                selected_ids.append(candidates[0].id)
            if selected_ids[0] == selected_ids[1]: return 'route_unavailable'
            return 'route_profile_ready'
        except (ProviderStoreError, ValueError, TypeError, KeyError):
            return 'route_unavailable'

    def human_cross_model_policy(self, admission, route_id):
        with self._human_cross_model_transaction(admission, write=False) as (db, connection):
            route = self._require_route(db, admission.owner, _required(route_id, 'route_id'))
            policy = self._cross_model_policy(connection, route.id) if connection is not None else None
            readiness=self._human_cross_model_readiness(db,connection,admission,route,policy)
            return {'supported':connection is not None, 'route_revision':int(route.revision),
                    'policy':policy, 'saved_enabled':bool(policy and policy['enabled']), 'runtime_available':False,
                    'route_profile_readiness':readiness}

    def set_human_cross_model_policy(self, admission, route_id, expected_route_revision, policy):
        if type(expected_route_revision) is not int or expected_route_revision < 1:
            raise ProviderValidationError('cross-model route revision unavailable')
        with self._human_cross_model_transaction(admission, write=True) as (db, connection):
            saved = self._set_cross_model_policy_tx(db, connection, admission, route_id, expected_route_revision, policy)
            route=self._require_route(db,admission.owner,route_id)
            readiness=self._human_cross_model_readiness(db,connection,admission,route,saved)
            return {'supported':True, 'route_revision':expected_route_revision+1,
                    'policy':saved, 'saved_enabled':saved['enabled'], 'runtime_available':False,
                    'route_profile_readiness':readiness}

    def capture_human_cross_model_selection(self, admission, chat_id, selected_route, candidate):
        """Capture an enabled owned route only under the original human request."""
        from src.openclank.chat_routing import NormalizedChatRoute
        from src.openclank.provider_human_policy import _captured_provider_policy
        from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
        from types import SimpleNamespace
        if (type(chat_id) is not str or not chat_id or len(chat_id) > 256
                or type(selected_route) is not NormalizedChatRoute or selected_route.shared):
            raise ProviderValidationError('captured provider route unavailable')
        with self._human_cross_model_transaction(admission, write=False) as (db, connection):
            if connection is None: return None
            from core.database import Session as DbSession
            chat = db.query(DbSession.owner, DbSession.provider_model_route_id).filter(DbSession.id == chat_id).first()
            if chat is None or chat.owner != admission.owner or chat.provider_model_route_id != selected_route.model_route_id:
                raise ProviderValidationError('captured provider chat unavailable')
            route = self._require_route(db, admission.owner, selected_route.model_route_id)
            provider = self._require_connection(db, admission.owner, route.connection_id)
            if (not route.enabled or not provider.enabled or selected_route.connection_id != provider.id
                    or selected_route.provider_model_id != route.provider_model_id or selected_route.family_id != provider.family_id
                    or selected_route.adapter_id != provider.adapter_id or selected_route.connection_kind != provider.kind
                    or selected_route.billing_lane != provider.billing_lane or selected_route.catalog_revision != route.catalog_revision
                    or selected_route.option_route_id != route.id or dict(selected_route.capabilities) != dict(route.capabilities or {})):
                raise ProviderValidationError('captured provider route changed')
            policy = self._cross_model_policy(connection, route.id)
            if policy is None or not policy['enabled']: return None
            if (type(candidate) is not dict or set(candidate) != {'route_id','catalog_revision','options','provider_model_route_id','provider_model_id'}
                    or candidate['route_id'] != route.id or candidate['catalog_revision'] != route.catalog_revision
                    or candidate['provider_model_route_id'] != route.id or candidate['provider_model_id'] != route.provider_model_id):
                raise ProviderValidationError('captured provider options unavailable')
            selected = validate_request_option_selection(route_id=route.id, catalog_revision=route.catalog_revision,
                descriptors=project_request_options(route.capabilities, catalog_revision=route.catalog_revision, source='engine-catalog'),
                selection={'route_id':candidate['route_id'],'catalog_revision':candidate['catalog_revision'],'options':candidate['options']})
            if selected != candidate['options']: raise ProviderValidationError('captured provider options changed')
            alternatives = self._cross_model_alternatives(db, admission, SimpleNamespace(connection_id=provider.id,
                model_id=route.provider_model_id, model_route_id=route.id, billing_lane=provider.billing_lane), policy)
            encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
            return _captured_provider_policy(admission, owner=admission.owner, account_id=admission.account_id, chat_id=chat_id,
                route_id=route.id, route_revision=route.revision, connection_id=provider.id, connection_revision=provider.revision,
                model_id=route.provider_model_id, catalog_revision=route.catalog_revision, candidate_json=encode(candidate),
                policy_json=encode(policy), alternatives_json=encode(alternatives))

    def _cross_model_binding_identity(self, db, admission, binding_id):
        row = db.query(ProviderOperationBinding).filter_by(id=_required(binding_id, 'binding_id'), owner=admission.owner).first()
        if (row is None or row.actor_id != 'main' or row.engine_session_id != admission.session_id
                or row.root_operation_id != admission.root_id or row.execution_revision != admission.execution_revision
                or row.operation_id is not None or not row.model_route_id
                or row.connection_id != admission.provider_id or row.model_id != admission.model_id):
            raise ProviderValidationError('cross-model binding unavailable')
        return row

    def _cross_model_binding(self, db, admission, binding_id):
        row = self._cross_model_binding_identity(db, admission, binding_id)
        matches = db.query(ProviderModelRoute.id).filter_by(owner=row.credential_owner, connection_id=row.connection_id,
            provider_model_id=row.model_id, enabled=True, deleted_at=None).limit(2).all()
        if len(matches) != 1 or matches[0][0] != row.model_route_id:
            raise ProviderValidationError('cross-model route unavailable')
        context = json.loads(admission.option_context_json)
        candidate, actor_options = context['mainOptionCandidate'], context['modelRequestOptions']
        options = (row.policy_snapshot or {}).get('main_model_options')
        if candidate is None:
            if options is not None: raise ProviderValidationError('cross-model main options unavailable')
        elif (type(candidate) is not dict or type(options) is not dict
                or options.get('routeID') != candidate.get('provider_model_route_id')
                or options.get('model', {}).get('modelID') != candidate.get('provider_model_id')
                or options.get('model', {}).get('providerID') != admission.provider_id
                or options.get('optionRouteID') != candidate.get('route_id')
                or options.get('catalogRevision') != candidate.get('catalog_revision')
                or options.get('options') != candidate.get('options')):
            raise ProviderValidationError('cross-model main options unavailable')
        if actor_options is not None and actor_options != options:
            raise ProviderValidationError('cross-model actor options unavailable')
        if options is not None:
            from src.openclank.chat_routing import _shared_request_option_route_id
            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
            route = self._require_route(db, row.credential_owner, row.model_route_id)
            option_id = _shared_request_option_route_id(admission.owner, row.grant_id, route.id) if row.grant_id else route.id
            descriptors = project_request_options(route.capabilities, catalog_revision=route.catalog_revision, source='engine-catalog')
            selected = validate_request_option_selection(route_id=option_id, catalog_revision=route.catalog_revision,
                descriptors=descriptors, selection={'route_id':options.get('optionRouteID'),
                    'catalog_revision':options.get('catalogRevision'), 'options':options.get('options')})
            if selected != options.get('options'):
                raise ProviderValidationError('cross-model main options changed')
        self._current_credential_authority(db, row, row.credential_revision)
        return row

    @staticmethod
    def _cross_model_primary(db, binding):
        route = db.query(ProviderModelRoute).filter_by(id=binding.model_route_id, owner=binding.credential_owner).one()
        connection = db.query(ProviderConnection).filter_by(id=binding.connection_id, owner=binding.credential_owner).one()
        grant = db.query(ProviderShareGrant).filter_by(id=binding.grant_id).first() if binding.grant_id else None
        return {'binding_id':binding.id, 'binding_revision':binding.revision, 'route_id':binding.model_route_id,
            'route_revision':route.revision, 'catalog_revision':route.catalog_revision,
            'connection_id':binding.connection_id, 'connection_revision':connection.revision,
            'model_id':binding.model_id, 'lane':binding.billing_lane, 'account_id':binding.selected_account_id,
            'credential_revision':binding.credential_revision, 'grant_id':binding.grant_id,
            'grant_revision':grant.revision if grant else None,
            'options':copy.deepcopy((binding.policy_snapshot or {}).get('main_model_options'))}

    def enroll_cross_model(self, admission, callback_authority, primary_binding_id, expected_binding_revision):
        if type(expected_binding_revision) is not int or expected_binding_revision < 1:
            raise ProviderValidationError('cross-model binding revision unavailable')
        with self._cross_model_transaction(admission, callback_authority, absent_ok=True) as (db, connection):
            if connection is None: return None
            binding = self._cross_model_binding(db, admission, primary_binding_id)
            if binding.grant_id is not None or binding.credential_owner != admission.owner:
                raise ShareDenied('cross-model primary requires owner consent')
            self._require_revision(binding.revision, expected_binding_revision)
            if binding.state != 'uncommitted': raise ProviderConflict('cross-model binding already committed')
            policy = self._cross_model_policy(connection, binding.model_route_id)
            if policy is None or not policy['enabled']: return None
            # A sticky binding may already have disclosed a credential.  Only the
            # later fresh-binding transaction can safely create a coordinator.
            raise ProviderValidationError('fresh cross-model enrollment unavailable')

    def _read_provisional_cross_model(self, db, connection, admission, binding_id, expected_binding_revision):
        """Validate the separately shaped fresh-root record; it grants no dispatch."""
        root, attachment = admission._root, admission._attachment
        if root is None or attachment is None or root.foreground_attachment is not attachment:
            raise ProviderValidationError('original provider root unavailable')
        binding = self._cross_model_binding(db, admission, binding_id)
        if (binding.grant_id is not None or binding.credential_owner != admission.owner
                or binding.state != 'uncommitted' or binding.revision != expected_binding_revision):
            raise ProviderValidationError('provisional primary unavailable')
        raw = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (binding.id,)).scalar_one()
        try:
            if type(raw) is not str or len(raw.encode('utf-8')) > 65536: raise ValueError()
            value = json.loads(raw)
        except (TypeError, ValueError, UnicodeError):
            raise ProviderValidationError('provisional coordinator unavailable') from None
        if (type(value) is not dict or set(value) != {'version','revision','state','original','primary','policy','alternatives','active_index'}
                or type(value.get('version')) is not int or value['version'] != 1
                or type(value.get('revision')) is not int or value['revision'] != 1 or value.get('state') != 'provisional'
                or type(value.get('active_index')) is not int or value['active_index'] != 0):
            raise ProviderValidationError('provisional coordinator unavailable')
        context = json.loads(admission.option_context_json)
        candidate = json.dumps(context['mainOptionCandidate'], sort_keys=True, separators=(',', ':'), allow_nan=False)
        original = {'owner':admission.owner, 'account_id':admission.account_id, 'chat_id':admission.chat_id,
            'session_id':admission.session_id, 'root_id':admission.root_id, 'holder_id':admission.holder_id,
            'generation':admission.generation, 'owner_epoch':admission.owner_epoch,
            'root_nonce':root.provider_root_nonce, 'actor_id':'main', 'execution_revision':admission.execution_revision,
            'options_sha256':hashlib.sha256(candidate.encode('utf-8')).hexdigest()}
        from src.openclank.provider_cross_model import validate_policy
        policy = validate_policy(value['policy'])
        if (value['original'] != original or value['primary'] != self._cross_model_primary(db, binding)
                or context['modelRequestOptions'] != (binding.policy_snapshot or {}).get('main_model_options')
                or not policy['enabled'] or self._cross_model_policy(connection, binding.model_route_id) != policy
                or self._cross_model_alternatives(db, admission, binding, policy) != value['alternatives']):
            raise ProviderValidationError('provisional authority changed')
        return binding, value, raw

    def _prepare_cross_model_admission(self, db, connection, admission, primary_binding_id, expected_binding_revision):
        """Revalidate the same fresh root, without committing or issuing."""
        if admission._root is None or admission._attachment is None:
            raise ProviderValidationError('original provider root unavailable')
        binding = self._cross_model_binding(db, admission, primary_binding_id)
        self._require_revision(binding.revision, expected_binding_revision)
        existing = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (binding.id,)).scalar_one()
        try: current = json.loads(existing) if type(existing) is str and len(existing.encode('utf-8')) <= 65536 else None
        except (ValueError, TypeError, UnicodeError): current = None
        if type(current) is dict and current.get('state') == 'admitted':
            _, value = self._read_cross_model(db, connection, admission, primary_binding_id)
            if (value['revision'] != 2 or self._cross_model_policy(connection, binding.model_route_id) != value['policy']
                    or value['original'].get('root_nonce') != admission._root.provider_root_nonce
                    or not value['original'].get('root_nonce')):
                raise ProviderConflict('admitted coordinator changed')
            return binding, value, existing, True
        binding, value, old = self._read_provisional_cross_model(db, connection, admission, primary_binding_id, expected_binding_revision)
        value['state'] = 'admitted'; value['revision'] = 2; value['original'] = self._cross_model_original(admission)
        return binding, value, old, False

    def admit_provisional_cross_model(self, admission, callback_authority, primary_binding_id,
                                      expected_binding_revision, expected_coordinator_revision):
        """One private F2 CAS; no credential, dispatch or route-advance authority."""
        if (type(expected_binding_revision) is not int or expected_binding_revision < 1
                or type(expected_coordinator_revision) is not int or expected_coordinator_revision != 1):
            raise ProviderValidationError('provisional revision unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            binding, value, old, admitted = self._prepare_cross_model_admission(db, connection, admission, primary_binding_id, expected_binding_revision)
            if admitted: return value
            new = json.dumps(value, sort_keys=True, separators=(',', ':'))
            from src.openclank.provider_cross_model import set_writer
            def authorize(op, bid, owner, root_id, actor, prior, replacement):
                try:
                    admission.assert_current()
                    return int(not callback_authority._generation_closed.is_set() and admission._callback is callback_authority
                        and callback_authority._store is self and
                        (op,bid,owner,root_id,actor,prior,replacement) ==
                        ('update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,old,new))
                except Exception: return 0
            set_writer(connection, authorize)
            try:
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                    (new,binding.id,expected_binding_revision,old))
                if changed.rowcount != 1: raise ProviderConflict('provisional coordinator changed')
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            return value

    def _read_cross_model(self, db, connection, admission, binding_id, *, terminal=False):
        binding = (self._cross_model_binding_identity(db, admission, binding_id) if terminal
                   else self._cross_model_binding(db, admission, binding_id))
        raw = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (binding.id,)).scalar_one()
        if raw is None: return binding, None
        try:
            if type(raw) is not str or len(raw.encode('utf-8')) > 65536:
                raise ValueError('coordinator too large')
            value = json.loads(raw)
        except (ValueError, TypeError, UnicodeError): raise ProviderValidationError('cross-model coordinator unavailable') from None
        if (type(value) is not dict or set(value) != {'version','revision','state','original','primary','policy','alternatives','active_index'}
                or type(value['version']) is not int or value['version'] != 1 or type(value['revision']) is not int or value['revision'] < 1
                or value['state'] not in {'admitted','committed','unknown'} or type(value['active_index']) is not int or value['active_index'] != 0
                or value['original'] != self._cross_model_original(admission) or type(value['primary']) is not dict
                or value['primary'].get('binding_id') != binding.id):
            raise ProviderValidationError('cross-model coordinator unavailable')
        primary = value['primary']
        primary_keys = {'binding_id','binding_revision','route_id','route_revision','catalog_revision','connection_id','connection_revision',
                        'model_id','lane','account_id','credential_revision','grant_id','grant_revision','options'}
        expected = {'binding_id':binding.id, 'route_id':binding.model_route_id, 'connection_id':binding.connection_id,
            'model_id':binding.model_id, 'lane':binding.billing_lane, 'grant_id':binding.grant_id}
        if not terminal:
            expected.update(account_id=binding.selected_account_id, credential_revision=binding.credential_revision)
        if (set(primary) != primary_keys or any(primary[k] != v for k, v in expected.items())
                or type(primary['binding_revision']) is not int or type(binding.revision) is not int
                or binding.revision < primary['binding_revision']
                or (not terminal and binding.revision not in (primary['binding_revision'], primary['binding_revision'] + (1 if binding.state == 'committed' else 0)))):
            raise ProviderValidationError('cross-model binding changed')
        from src.openclank.provider_cross_model import validate_policy
        policy = validate_policy(value['policy'])
        alternatives = value['alternatives']
        keys = {'route_id','route_revision','connection_id','connection_revision','provider_id','model_id','lane','grant_id','grant_revision','options'}
        if (not policy['enabled'] or type(alternatives) is not list or len(alternatives) != len(policy['alternatives'])
                or any(type(item) is not dict or set(item) != keys or item['route_id'] != configured['route_id']
                    or item['options'] != configured['options'] for item, configured in zip(alternatives, policy['alternatives']))):
            raise ProviderValidationError('cross-model authority changed')
        if not terminal:
            current = self._cross_model_primary(db, binding)
            current.pop('binding_revision')
            frozen = dict(primary); frozen.pop('binding_revision')
            if current != frozen or self._cross_model_alternatives(db, admission, binding, policy) != value['alternatives']:
                raise ProviderValidationError('cross-model authority changed')
        return binding, value

    def read_cross_model(self, admission, callback_authority, primary_binding_id):
        with self._cross_model_transaction(admission, callback_authority, absent_ok=True) as (db, connection):
            if connection is None: return None
            return self._read_cross_model(db, connection, admission, primary_binding_id)[1]

    def mark_cross_model(self, admission, callback_authority, primary_binding_id, expected_coordinator_revision, state):
        if state not in {'committed','unknown'}: raise ProviderValidationError('cross-model transition unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            binding, value = self._read_cross_model(db, connection, admission, primary_binding_id, terminal=True)
            if value is None or type(expected_coordinator_revision) is not int or value['revision'] != expected_coordinator_revision:
                raise ProviderConflict('cross-model coordinator changed')
            if value['state'] != 'admitted' or (state == 'committed' and binding.state != 'committed'):
                raise ProviderConflict('cross-model transition unavailable')
            old = json.dumps(value, sort_keys=True, separators=(',', ':'))
            value['state'] = state
            value['revision'] += 1
            new = json.dumps(value, sort_keys=True, separators=(',', ':'))
            from src.openclank.provider_cross_model import set_writer
            def authorize(op, binding_id, owner, root, actor, prior, replacement):
                try:
                    admission.assert_current()
                    return int(not callback_authority._generation_closed.is_set()
                        and admission._callback is callback_authority and callback_authority._store is self
                        and (op, binding_id, owner, root, actor, prior, replacement) ==
                        ('update', binding.id, binding.owner, binding.root_operation_id, binding.actor_scope_key, old, new))
                except Exception:
                    return 0
            set_writer(connection, authorize)
            try:
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                    (new, binding.id, binding.revision, old))
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            if changed.rowcount != 1: raise ProviderConflict('cross-model coordinator changed')
            return value

    @staticmethod
    def _reject_enrolled_scope(db, owner, root, actor_scope):
        from src.openclank.provider_cross_model import profile_absent, profile_ready
        connection = db.connection()
        if connection.dialect.name != 'sqlite':
            return
        if profile_absent(connection):
            return
        if not profile_ready(connection):
            raise ProviderValidationError('cross-model profile unavailable')
        found = connection.exec_driver_sql(
            'SELECT 1 FROM provider_operation_bindings WHERE owner=? AND root_operation_id=? AND cross_model_coordinator IS NOT NULL LIMIT 1',
            (owner, root)).first()
        if found:
            raise ProviderValidationError('enrolled provider operation unavailable')

    def _reject_enrolled(self, db, binding):
        self._reject_enrolled_scope(db, binding.owner, binding.root_operation_id, binding.actor_scope_key)

    @staticmethod
    def _unenrolled_lease_query(db, query):
        """Keep lifecycle cleanup from deleting a lease protected by either root identity."""
        from src.openclank.provider_cross_model import profile_absent, profile_ready
        connection=db.connection()
        if connection.dialect.name != 'sqlite' or profile_absent(connection): return query
        if not profile_ready(connection): raise ProviderValidationError('cross-model profile unavailable')
        from sqlalchemy import text
        return query.filter(text('NOT EXISTS (SELECT 1 FROM provider_operation_bindings p '
            'WHERE p.cross_model_coordinator IS NOT NULL AND '
            '((p.owner=provider_credential_leases.owner AND p.root_operation_id=provider_credential_leases.root_operation_id) '
            'OR EXISTS (SELECT 1 FROM provider_operation_bindings b WHERE b.id=provider_credential_leases.binding_id '
            'AND b.owner=p.owner AND b.root_operation_id=p.root_operation_id)))'))

    @staticmethod
    def _invalidate_credential_leases(db, *, owner, connection_id, account_id=None, model_id=None, grant_id=None):
        query=db.query(ProviderCredentialLease).filter_by(credential_owner=owner,connection_id=connection_id)
        if account_id is not None: query=query.filter_by(account_id=account_id)
        if model_id is not None: query=query.filter_by(model_id=model_id)
        if grant_id is not None: query=query.filter_by(grant_id=grant_id)
        ProviderStore._unenrolled_lease_query(db,query).delete(synchronize_session=False)

    def _invalidate_entitlement_lease(self, db, *, owner, account, route_id):
        route=db.query(ProviderModelRoute).filter_by(id=route_id,owner=owner,connection_id=account.connection_id).first()
        if route is not None:
            self._invalidate_credential_leases(db,owner=owner,connection_id=account.connection_id,account_id=account.id,model_id=route.provider_model_id)

    def _current_credential_authority(self, db, binding, credential_revision):
        if binding.grant_id is None and binding.owner != binding.credential_owner:
            raise ShareDenied('provider credential owner is unavailable')
        connection=self._require_connection(db,binding.credential_owner,binding.connection_id)
        if not connection.enabled or binding.provider_id!=connection.family_id or binding.billing_lane!=connection.billing_lane:
            raise NoEligibleAccount('provider connection is unavailable')
        if not binding.selected_account_id:
            raise ProviderNotFound('provider binding has no credential account')
        account=self._require_account(db,binding.credential_owner,binding.selected_account_id)
        if not account.enabled or account.connection_id!=connection.id or not account.credential_envelope:
            raise NoEligibleAccount('provider account is unavailable')
        self._require_revision(binding.credential_revision,credential_revision)
        self._require_revision(account.credential_version,credential_revision)
        route=db.query(ProviderModelRoute).filter_by(owner=binding.credential_owner,connection_id=connection.id,
            id=binding.model_route_id,provider_model_id=binding.model_id,enabled=True,deleted_at=None).first()
        if route is None: raise ProviderNotFound('provider model route is unavailable')
        entitlement=db.query(ProviderAccountEntitlement).filter_by(account_id=account.id,model_route_id=route.id,eligible=True).first()
        if entitlement is None: raise NoEligibleAccount('bound account is not entitled to the requested model')
        if binding.grant_id is not None:
            grant=db.query(ProviderShareGrant).filter_by(id=binding.grant_id,owner=binding.credential_owner,
                recipient=binding.owner,connection_id=connection.id,billing_lane=binding.billing_lane,state='active').first()
            if grant is None or not AccountSelector.parse(grant.account_selector).includes(account.id) or not ModelSelector.parse(grant.model_selector).includes(route.id):
                raise ShareDenied('provider share grant is unavailable')
        if connection.family_id=='anthropic' and connection.billing_lane=='subscription':
            _check_method_policy(_binding_method_policy(binding),binding.credential_owner,account)
        return account

    def revoke_worker_generation(self, *, owner: str, holder_id: str) -> None:
        """Revoke leases for a trusted, unique supervisor-issued generation."""
        owner = _owner(owner)
        holder_id = _required(holder_id, "holder_id")
        if not re.fullmatch(r"pwg_[0-9a-f]{32}", holder_id):
            raise ProviderValidationError("invalid worker generation")
        with self._transaction() as db:
            self._unenrolled_lease_query(db,db.query(ProviderCredentialLease).filter_by(owner=owner, holder_id=holder_id)).delete(synchronize_session=False)
            # Refresh rows have no consumer-owner column. The unique holder is
            # supplied by the trusted callback instance, never by the client.
            db.query(ProviderRefreshLease).filter_by(holder_id=holder_id).delete(synchronize_session=False)

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

    def list_agent_comfy_profiles(self, *, owner: str) -> dict:
        """Bounded own-connection metadata; never disclose URLs or credentials."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label)
                .filter(ProviderConnection.owner == owner, ProviderConnection.family_id == 'comfyui',
                        ProviderConnection.adapter_id == 'comfyui-workflow-v1', ProviderConnection.enabled.is_(True),
                        ProviderConnection.deleted_at.is_(None))
                .order_by(ProviderConnection.id).limit(21).all())
            values=[]
            for connection_id, revision, label in rows[:20]:
                if (not isinstance(connection_id,str) or not connection_id or len(connection_id)>256
                    or type(revision) is not int or revision<1 or not isinstance(label,str)
                    or len(label.encode('utf-8'))>512):
                    raise ProviderValidationError('media profiles unavailable')
                values.append({'connection_id':connection_id,'revision':revision,'label':label})
            return {'profiles':values,'partial':len(rows)>20}

    def list_human_tripo_profiles(self, *, owner: str) -> dict:
        """Bounded own API-key account candidates; no credential material leaves this query."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label,
                             ProviderAccount.id, ProviderAccount.revision, ProviderAccount.credential_version, ProviderAccount.label)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner,
                        ProviderConnection.family_id == 'tripo', ProviderConnection.adapter_id == 'tripo-text3d-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None), ProviderAccount.credential_envelope != '')
                .order_by(ProviderConnection.id, ProviderAccount.id).limit(21).all())
            result = []
            for connection_id, connection_revision, connection_label, account_id, account_revision, credential_version, account_label in rows[:20]:
                if (any(not isinstance(v, str) or not v or len(v) > 256 for v in (connection_id, account_id))
                        or any(type(v) is not int or v < 1 for v in (connection_revision, account_revision, credential_version))
                        or any(not isinstance(v, str) or len(v.encode('utf-8')) > 512 for v in (connection_label, account_label))):
                    raise ProviderValidationError('media profiles unavailable')
                result.append({'connection_id':connection_id, 'connection_revision':connection_revision,
                    'account_id':account_id, 'account_revision':account_revision, 'credential_version':credential_version,
                    'label':f'{connection_label} / {account_label}'})
            return {'profiles':result, 'partial':len(rows) > 20}

    def list_human_meshy_profiles(self, *, owner: str) -> dict:
        """Bounded own API-key account candidates; no credential material leaves this query."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label,
                             ProviderAccount.id, ProviderAccount.revision, ProviderAccount.credential_version, ProviderAccount.label)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner,
                        ProviderConnection.family_id == 'meshy', ProviderConnection.adapter_id == 'meshy-preview-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None), ProviderAccount.credential_envelope != '')
                .order_by(ProviderConnection.id, ProviderAccount.id).limit(21).all())
            result = []
            for connection_id, connection_revision, connection_label, account_id, account_revision, credential_version, account_label in rows[:20]:
                if (any(not isinstance(v, str) or not v or len(v) > 256 for v in (connection_id, account_id))
                        or any(type(v) is not int or v < 1 for v in (connection_revision, account_revision, credential_version))
                        or any(not isinstance(v, str) or len(v.encode('utf-8')) > 512 for v in (connection_label, account_label))):
                    raise ProviderValidationError('media profiles unavailable')
                result.append({'connection_id':connection_id, 'connection_revision':connection_revision,
                    'account_id':account_id, 'account_revision':account_revision, 'credential_version':credential_version,
                    'label':f'{connection_label} / {account_label}'})
            return {'profiles':result, 'partial':len(rows) > 20}

    def list_agent_trellis_profiles(self, *, owner: str) -> dict:
        """Bounded owned local selectors, without installation or credential data."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label)
                .filter(ProviderConnection.owner == owner, ProviderConnection.family_id == 'trellis',
                        ProviderConnection.adapter_id == 'trellis-image3d-v1', ProviderConnection.kind == 'local',
                        ProviderConnection.billing_lane == 'local', ProviderConnection.enabled.is_(True),
                        ProviderConnection.deleted_at.is_(None)).order_by(ProviderConnection.id).limit(21).all())
            profiles=[]
            for connection_id,revision,label in rows[:20]:
                if (not isinstance(connection_id,str) or not connection_id or len(('trellis:'+connection_id).encode('utf-8'))>256
                        or type(revision) is not int or revision<1 or not isinstance(label,str) or len(label.encode('utf-8'))>512):
                    raise ProviderValidationError('media profiles unavailable')
                profiles.append({'connection_id':connection_id,'revision':revision,'label':label})
            return {'profiles':profiles,'partial':len(rows)>20}

    def resolve_trellis_media_selector(self, *, owner: str, selector: str):
        """Resolve exact owned local and eligible legacy Fal metadata without credential bytes."""
        owner = _owner(owner)
        if type(selector) is not str or not selector.startswith('trellis:') or not selector[len('trellis:'):] or len(selector.encode('utf-8'))>256:
            raise ProviderValidationError('media selector unavailable')
        suffix = selector[len('trellis:'):]
        with self._transaction() as db:
            local = db.query(ProviderConnection.id, ProviderConnection.family_id).filter(
                ProviderConnection.owner == owner, ProviderConnection.id == suffix).first()
            remote = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderAccount.id,
                               ProviderAccount.revision, ProviderAccount.credential_version)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner, ProviderAccount.id == selector,
                        ProviderConnection.family_id == 'fal', ProviderConnection.adapter_id == 'fal-flux-schnell-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None),
                        ProviderAccount.credential_envelope != '').first())
            local_exists = local is not None and local.family_id == 'trellis'
            selected = None if remote is None else {'connection_id':remote[0], 'connection_revision':remote[1],
                'account_id':remote[2], 'account_revision':remote[3], 'credential_version':remote[4]}
            return local_exists, selected

    def list_agent_hunyuan_profiles(self, *, owner: str) -> dict:
        """Bounded owned local selectors, without installation or credential data."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label)
                .filter(ProviderConnection.owner == owner, ProviderConnection.family_id == 'hunyuan-shape',
                        ProviderConnection.adapter_id == 'hunyuan3d-v2-shape-v1', ProviderConnection.kind == 'local',
                        ProviderConnection.billing_lane == 'local', ProviderConnection.enabled.is_(True),
                        ProviderConnection.deleted_at.is_(None)).order_by(ProviderConnection.id).limit(21).all())
            profiles=[]
            for connection_id,revision,label in rows[:20]:
                if (not isinstance(connection_id,str) or not connection_id or len(('hunyuan:'+connection_id).encode('utf-8'))>256
                        or type(revision) is not int or revision<1 or not isinstance(label,str) or len(label.encode('utf-8'))>512):
                    raise ProviderValidationError('media profiles unavailable')
                profiles.append({'connection_id':connection_id,'revision':revision,'label':label})
            return {'profiles':profiles,'partial':len(rows)>20}

    def resolve_hunyuan_media_selector(self, *, owner: str, selector: str):
        """Resolve exact owned local and eligible legacy Fal metadata without credential bytes."""
        owner = _owner(owner)
        if type(selector) is not str or not selector.startswith('hunyuan:') or not selector[len('hunyuan:'):] or len(selector.encode('utf-8'))>256:
            raise ProviderValidationError('media selector unavailable')
        suffix = selector[len('hunyuan:'):]
        with self._transaction() as db:
            local = db.query(ProviderConnection.id, ProviderConnection.family_id).filter(
                ProviderConnection.owner == owner, ProviderConnection.id == suffix).first()
            remote = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderAccount.id,
                               ProviderAccount.revision, ProviderAccount.credential_version)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner, ProviderAccount.id == selector,
                        ProviderConnection.family_id == 'fal', ProviderConnection.adapter_id == 'fal-flux-schnell-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None),
                        ProviderAccount.credential_envelope != '').first())
            local_exists = local is not None and local.family_id == 'hunyuan-shape'
            selected = None if remote is None else {'connection_id':remote[0], 'connection_revision':remote[1],
                'account_id':remote[2], 'account_revision':remote[3], 'credential_version':remote[4]}
            return local_exists, selected

    def list_human_fal_profiles(self, *, owner: str) -> dict:
        """Bounded own API-key account candidates; no credential material leaves this query."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label,
                             ProviderAccount.id, ProviderAccount.revision, ProviderAccount.credential_version, ProviderAccount.label)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner,
                        ProviderConnection.family_id == 'fal', ProviderConnection.adapter_id == 'fal-flux-schnell-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None), ProviderAccount.credential_envelope != '')
                .order_by(ProviderConnection.id, ProviderAccount.id).limit(21).all())
            result = []
            for connection_id, connection_revision, connection_label, account_id, account_revision, credential_version, account_label in rows[:20]:
                if (any(not isinstance(v, str) or not v or len(v) > 256 for v in (connection_id, account_id))
                        or any(type(v) is not int or v < 1 for v in (connection_revision, account_revision, credential_version))
                        or any(not isinstance(v, str) or len(v.encode('utf-8')) > 512 for v in (connection_label, account_label))):
                    raise ProviderValidationError('media profiles unavailable')
                result.append({'connection_id':connection_id, 'connection_revision':connection_revision,
                    'account_id':account_id, 'account_revision':account_revision, 'credential_version':credential_version,
                    'label':f'{connection_label} / {account_label}'})
            return {'profiles':result, 'partial':len(rows) > 20}

    def list_human_voidless_profiles(self, *, owner: str) -> dict:
        """Bounded own API-key account candidates; no credential material leaves this query."""
        owner = _owner(owner)
        with self._transaction() as db:
            rows = (db.query(ProviderConnection.id, ProviderConnection.revision, ProviderConnection.label,
                             ProviderAccount.id, ProviderAccount.revision, ProviderAccount.credential_version, ProviderAccount.label)
                .join(ProviderAccount, ProviderAccount.connection_id == ProviderConnection.id)
                .filter(ProviderConnection.owner == owner, ProviderAccount.owner == owner,
                        ProviderConnection.family_id == 'voidless', ProviderConnection.adapter_id == 'voidless-image-v1',
                        ProviderConnection.kind == 'official', ProviderConnection.billing_lane == 'metered_api',
                        ProviderConnection.enabled.is_(True), ProviderConnection.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.auth_method == 'api_key', ProviderAccount.auth_class == 'metered',
                        ProviderAccount.acquisition_method == 'manual-key', ProviderAccount.credential_envelope.isnot(None), ProviderAccount.credential_envelope != '')
                .order_by(ProviderConnection.id, ProviderAccount.id).limit(21).all())
            result = []
            for connection_id, connection_revision, connection_label, account_id, account_revision, credential_version, account_label in rows[:20]:
                if (any(not isinstance(v, str) or not v or len(v) > 256 for v in (connection_id, account_id))
                        or any(type(v) is not int or v < 1 for v in (connection_revision, account_revision, credential_version))
                        or any(not isinstance(v, str) or len(v.encode('utf-8')) > 512 for v in (connection_label, account_label))):
                    raise ProviderValidationError('media profiles unavailable')
                result.append({'connection_id':connection_id, 'connection_revision':connection_revision,
                    'account_id':account_id, 'account_revision':account_revision, 'credential_version':credential_version,
                    'label':f'{connection_label} / {account_label}'})
            return {'profiles':result, 'partial':len(rows) > 20}

    def update_connection(
        self,
        *,
        owner: str,
        connection_id: str,
        expected_revision: int,
        label: Any = _MISSING,
        normalized_url: Any = _MISSING,
        settings: Any = _MISSING,
        account_pool_policy: Any = _MISSING,
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
            changed_authority = (enabled is not _MISSING and bool(row.enabled) != bool(enabled))
            if normalized_url is not _MISSING:
                changed_authority = changed_authority or row.normalized_url != (str(normalized_url or '').strip() or None)
            if settings is not _MISSING:
                changed_authority = changed_authority or (row.settings or {}) != _json_mapping(settings)
            if changed_authority:
                self._invalidate_credential_leases(db,owner=owner,connection_id=connection_id)
            if label is not _MISSING:
                row.label = _required(label, "label")
            if normalized_url is not _MISSING:
                value = str(normalized_url or "").strip()
                row.normalized_url = value or None
            if settings is not _MISSING:
                row.settings = _json_mapping(settings)
            if account_pool_policy is not _MISSING:
                policy = _account_pool_policy(account_pool_policy)
                if policy is not None and policy.get("account_id"):
                    target = db.query(ProviderAccount.id).filter(
                        ProviderAccount.id == policy["account_id"],
                        ProviderAccount.owner == owner,
                        ProviderAccount.connection_id == row.id,
                    ).first()
                    if target is None:
                        raise ProviderValidationError(
                            "account_pool_policy account is outside this connection"
                        )
                row.account_pool_policy = policy
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
                _erase_optional_quota_rows(db, owner=owner, account_ids=account_ids)
                db.query(ProviderRefreshLease).filter(
                    ProviderRefreshLease.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id.in_(account_ids)
                ).delete(synchronize_session=False)
                self._unenrolled_lease_query(db,db.query(ProviderCredentialLease).filter(
                    ProviderCredentialLease.account_id.in_(account_ids)
                )).delete(synchronize_session=False)
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
        acquisition_method: Optional[str] = None,
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
                acquisition_method=_provider_acquisition_method(acquisition_method),
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
            enabled_changed = False
            if label is not _MISSING:
                row.label = _required(label, "label")
            if enabled is not _MISSING:
                enabled_changed = bool(row.enabled) != bool(enabled)
                pool_changed = enabled_changed
                row.enabled = bool(enabled)
            if enabled_changed:
                # A lease issued before disable must not revive on re-enable.
                self._unenrolled_lease_query(db,db.query(ProviderCredentialLease).filter(
                    ProviderCredentialLease.credential_owner == owner,
                    ProviderCredentialLease.account_id == account_id,
                )).delete(synchronize_session=False)
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
            self._unenrolled_lease_query(db,db.query(ProviderCredentialLease).filter(
                ProviderCredentialLease.account_id == row.id
            )).delete(synchronize_session=False)
            _erase_optional_quota_rows(db, owner=owner, account_ids=[row.id])
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

    def meshy_job_credential(self, *, owner: str, connection_id: str, connection_revision: int,
                             account_id: str, account_revision: int, credential_version: int) -> CredentialAccess:
        """Resolve one exact self-owned official Meshy job credential."""
        owner = _owner(owner)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, _required(connection_id, 'connection_id'))
            account = self._require_account(db, owner, _required(account_id, 'account_id'))
            if (connection.family_id != 'meshy' or connection.adapter_id != 'meshy-preview-v1'
                    or connection.kind != 'official' or connection.billing_lane != 'metered_api'
                    or connection.normalized_url != 'https://api.meshy.ai' or connection.settings != {'interface':'meshy-preview-v1'} or not connection.enabled
                    or int(connection.revision) != connection_revision or account.connection_id != connection.id
                    or not account.enabled or int(account.revision) != account_revision
                    or int(account.credential_version) != credential_version or account.auth_method != 'api_key'
                    or account.auth_class != 'metered' or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                raise ProviderNotFound('Meshy job credential unavailable')
            payload = unseal_credential(account.credential_envelope,
                CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
            key=payload.get('key')
            if (set(payload) != {'type','key'} or payload.get('type') != 'api' or type(key) is not str
                    or not key or len(key)>4096 or any(ord(c)<33 or ord(c)==127 for c in key)):
                raise ProviderNotFound('Meshy job credential unavailable')
            return CredentialAccess(account_id=account.id,connection_id=connection.id,
                account_revision=account.revision,credential_version=account.credential_version,credentials=payload)

    def tripo_job_credential(self, *, owner: str, connection_id: str, connection_revision: int,
                             account_id: str, account_revision: int, credential_version: int) -> CredentialAccess:
        """Resolve one exact self-owned official Tripo job credential."""
        owner = _owner(owner)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, _required(connection_id, 'connection_id'))
            account = self._require_account(db, owner, _required(account_id, 'account_id'))
            if (connection.family_id != 'tripo' or connection.adapter_id != 'tripo-text3d-v1'
                    or connection.kind != 'official' or connection.billing_lane != 'metered_api'
                    or connection.normalized_url != 'https://openapi.tripo3d.ai' or connection.settings != {'interface':'tripo-text3d-v1'} or not connection.enabled
                    or int(connection.revision) != connection_revision or account.connection_id != connection.id
                    or not account.enabled or int(account.revision) != account_revision
                    or int(account.credential_version) != credential_version or account.auth_method != 'api_key'
                    or account.auth_class != 'metered' or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                raise ProviderNotFound('Tripo job credential unavailable')
            payload = unseal_credential(account.credential_envelope,
                CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
            key=payload.get('key')
            if (set(payload) != {'type','key'} or payload.get('type') != 'api' or type(key) is not str
                    or not key or len(key)>4096 or any(ord(c)<33 or ord(c)==127 for c in key)):
                raise ProviderNotFound('Tripo job credential unavailable')
            return CredentialAccess(account_id=account.id,connection_id=connection.id,
                account_revision=account.revision,credential_version=account.credential_version,credentials=payload)

    def fal_job_credential(self, *, owner: str, connection_id: str, connection_revision: int,
                             account_id: str, account_revision: int, credential_version: int) -> CredentialAccess:
        """Resolve one exact self-owned official Fal job credential."""
        owner = _owner(owner)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, _required(connection_id, 'connection_id'))
            account = self._require_account(db, owner, _required(account_id, 'account_id'))
            if (connection.family_id != 'fal' or connection.adapter_id != 'fal-flux-schnell-v1'
                    or connection.kind != 'official' or connection.billing_lane != 'metered_api'
                    or connection.normalized_url != 'https://queue.fal.run' or connection.settings != {'interface':'fal-flux-schnell-v1'} or not connection.enabled
                    or int(connection.revision) != connection_revision or account.connection_id != connection.id
                    or not account.enabled or int(account.revision) != account_revision
                    or int(account.credential_version) != credential_version or account.auth_method != 'api_key'
                    or account.auth_class != 'metered' or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                raise ProviderNotFound('Fal job credential unavailable')
            payload = unseal_credential(account.credential_envelope,
                CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
            key=payload.get('key')
            if (set(payload) != {'type','key'} or payload.get('type') != 'api' or type(key) is not str
                    or not key or len(key)>4096 or any(ord(c)<33 or ord(c)==127 for c in key)):
                raise ProviderNotFound('Fal job credential unavailable')
            return CredentialAccess(account_id=account.id,connection_id=connection.id,
                account_revision=account.revision,credential_version=account.credential_version,credentials=payload)

    def voidless_job_credential(self, *, owner: str, connection_id: str, connection_revision: int,
                             account_id: str, account_revision: int, credential_version: int) -> CredentialAccess:
        """Resolve one exact self-owned official Voidless job credential."""
        owner = _owner(owner)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, _required(connection_id, 'connection_id'))
            account = self._require_account(db, owner, _required(account_id, 'account_id'))
            if (connection.family_id != 'voidless' or connection.adapter_id != 'voidless-image-v1'
                    or connection.kind != 'official' or connection.billing_lane != 'metered_api'
                    or connection.normalized_url != 'https://api.voidless.dev' or connection.settings != {'interface':'voidless-image-v1'} or not connection.enabled
                    or int(connection.revision) != connection_revision or account.connection_id != connection.id
                    or not account.enabled or int(account.revision) != account_revision
                    or int(account.credential_version) != credential_version or account.auth_method != 'api_key'
                    or account.auth_class != 'metered' or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                raise ProviderNotFound('Voidless job credential unavailable')
            payload = unseal_credential(account.credential_envelope,
                CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
            key=payload.get('key')
            if (set(payload) != {'type','key'} or payload.get('type') != 'api' or type(key) is not str
                    or not key or len(key)>4096 or any(ord(c)<33 or ord(c)==127 for c in key)):
                raise ProviderNotFound('Voidless job credential unavailable')
            return CredentialAccess(account_id=account.id,connection_id=connection.id,
                account_revision=account.revision,credential_version=account.credential_version,credentials=payload)

    def sprixen_job_credential(self, *, owner: str, connection_id: str, connection_revision: int,
                               account_id: str, account_revision: int, credential_version: int) -> CredentialAccess:
        """Disclose one self-owned exact job credential under the existing store transaction."""
        owner = _owner(owner)
        with self._transaction() as db:
            connection = self._require_connection(db, owner, _required(connection_id, 'connection_id'))
            account = self._require_account(db, owner, _required(account_id, 'account_id'))
            if (connection.family_id != 'sprixen' or connection.adapter_id != 'sprixen-sprite-v1'
                    or connection.kind != 'official' or connection.billing_lane != 'metered_api'
                    or connection.normalized_url != 'https://api.sprixen.com' or connection.settings != {'interface':'sprixen-sprite-v1'} or not connection.enabled
                    or int(connection.revision) != connection_revision or account.connection_id != connection.id
                    or not account.enabled or int(account.revision) != account_revision
                    or int(account.credential_version) != credential_version or account.auth_method != 'api_key'
                    or account.auth_class != 'metered' or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                raise ProviderNotFound('Sprixen job credential unavailable')
            payload = unseal_credential(account.credential_envelope,
                CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
            if set(payload) != {'type', 'key'} or payload.get('type') != 'api' or not isinstance(payload.get('key'), str) or not re.fullmatch(r'spx_live_[A-Za-z0-9_-]{1,512}', payload['key']):
                raise ProviderNotFound('Sprixen job credential unavailable')
            return CredentialAccess(account_id=account.id, connection_id=connection.id,
                account_revision=account.revision, credential_version=account.credential_version, credentials=payload)

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
                    if bool(row.enabled) != value["enabled"] or row.deleted_at is not None or list(row.operations or ()) != value["operations"]:
                        self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=row.provider_model_id)
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
                self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=row.provider_model_id)
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
                        if bool(entitlement.eligible) != bool(row.enabled):
                            self._invalidate_entitlement_lease(db,owner=owner,account=account,route_id=row.id)
                        entitlement.eligible = bool(row.enabled)
                        entitlement.evidence = {"authority": "managed-engine"}
                        entitlement.last_seen_at = timestamp
                    for route_id, entitlement in existing_entitlements.items():
                        if route_id in active_ids:
                            continue
                        if entitlement.eligible:
                            self._invalidate_entitlement_lease(db,owner=owner,account=account,route_id=route_id)
                        entitlement.eligible = False
                        entitlement.revision += 1
                        entitlement.last_seen_at = timestamp

            detached_connection = self._detach(db, connection)
            return detached_connection, self._detach_many(db, active_rows)

    def reconcile_account_discovery(
        self,
        *,
        owner: str,
        account_id: str,
        model_routes: Iterable[Mapping[str, Any]],
        status: str = "complete",
        authoritative: bool = True,
        error_code: Optional[str] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        expected_credential_revision: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> tuple[ProviderConnection, list[ProviderModelRoute]]:
        """Upsert connection metadata and reconcile exactly one account.

        Account discovery is a snapshot of one credential.  Route rows are
        connection metadata and therefore form the union seen by all sibling
        accounts; entitlement rows are the account-scoped snapshot.  A
        non-authoritative result leaves the last-good entitlement snapshot in
        place while recording a secret-free account health state.
        """

        owner = _owner(owner)
        account_id = _required(account_id, "account_id")
        normalized_status = _required(status, "status").lower()
        if normalized_status not in {"complete", "unavailable", "partial", "reauth_required"}:
            raise ProviderValidationError("unsupported account discovery status")
        if normalized_status == "complete" and not authoritative:
            raise ProviderValidationError("complete account discovery must be authoritative")
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
                    "display_name": _required(value.get("display_name") or model_id, "display_name"),
                    "operations": operations,
                    "capabilities": _json_mapping(value.get("capabilities")),
                    "provenance": _json_mapping(value.get("provenance")),
                    "enabled": bool(value.get("enabled", True)),
                }
            )

        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            account = self._require_account(db, owner, account_id)
            if (
                expected_credential_revision is not None
                and int(account.credential_version) != int(expected_credential_revision)
            ):
                raise ProviderRevisionConflict(
                    expected=int(expected_credential_revision),
                    current=int(account.credential_version),
                )
            connection = self._require_connection(db, owner, account.connection_id)
            connection.revision += 1
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
                    if bool(row.enabled) != value["enabled"] or row.deleted_at is not None or list(row.operations or ()) != value["operations"]:
                        self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=row.provider_model_id)
                    row.revision += 1
                row.display_name = value["display_name"]
                row.operations = value["operations"]
                row.capabilities = value["capabilities"]
                row.provenance = _merge_route_provenance(value["provenance"], provenance)
                row.catalog_revision = catalog_revision
                row.enabled = value["enabled"]
                row.deleted_at = None
                active_rows.append(row)

            if normalized_status == "complete":
                db.flush()
                active_ids = {row.id for row in active_rows if row.enabled}
                existing_entitlements = {
                    row.model_route_id: row
                    for row in db.query(ProviderAccountEntitlement).filter(
                        ProviderAccountEntitlement.account_id == account.id,
                    ).all()
                }
                evidence = {
                    "authority": "managed-engine",
                    "discovery_status": normalized_status,
                }
                evidence.update(_json_mapping(provenance))
                all_routes = {route.id: route for route in (*current_rows, *active_rows)}
                for route in all_routes.values():
                    existing = route.id in existing_entitlements
                    if existing:
                        entitlement = existing_entitlements[route.id]
                    else:
                        entitlement = ProviderAccountEntitlement(
                            account_id=account.id,
                            model_route_id=route.id,
                        )
                        db.add(entitlement)
                    if existing:
                        entitlement.revision += 1
                    if bool(entitlement.eligible) != (route.id in active_ids):
                        self._invalidate_entitlement_lease(db,owner=owner,account=account,route_id=route.id)
                    entitlement.eligible = route.id in active_ids
                    entitlement.evidence = evidence
                    entitlement.last_seen_at = timestamp
                for route in current_rows:
                    if route.id in active_ids:
                        continue
                    sibling = db.query(ProviderAccountEntitlement).join(
                        ProviderAccount,
                        ProviderAccount.id == ProviderAccountEntitlement.account_id,
                    ).filter(
                        ProviderAccountEntitlement.model_route_id == route.id,
                        ProviderAccountEntitlement.eligible.is_(True),
                        ProviderAccountEntitlement.account_id != account.id,
                        ProviderAccount.connection_id == connection.id,
                        ProviderAccount.deleted_at.is_(None),
                    ).first()
                    if sibling is None and route.deleted_at is None:
                        self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=route.provider_model_id)
                        route.enabled = False
                        route.deleted_at = timestamp
                        route.revision += 1

            health = db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id == account.id,
            ).first()
            if health is None:
                health = ProviderAccountHealth(account_id=account.id)
                db.add(health)
            else:
                health.revision += 1
            # A temporary inventory outage cannot clear an authentication failure.
            if health.state != "reauth_required" or normalized_status != "unavailable":
                health.state = {
                    "complete": "healthy",
                    "unavailable": "degraded",
                    "partial": "degraded",
                    "reauth_required": "reauth_required",
                }[normalized_status]
                health.last_error_code = _safe_error_code(error_code)
            if normalized_status == "complete":
                health.last_success_at = timestamp
            db.flush()
            return self._detach(db, connection), self._detach_many(db, active_rows)

    def persist_account_discovery(
        self,
        *,
        owner: str,
        account_id: str,
        connection_id: str,
        label: str,
        auth_method: str,
        acquisition_method: Optional[str] = None,
        auth_class: str,
        credentials: Mapping[str, Any],
        safe_identity: Optional[Mapping[str, Any]],
        model_routes: Iterable[Mapping[str, Any]],
        status: str = "complete",
        authoritative: bool = True,
        error_code: Optional[str] = None,
        provenance: Optional[Mapping[str, Any]] = None,
        expected_account_revision: Optional[int] = None,
        expected_credential_revision: int = 1,
        now: Optional[datetime] = None,
    ) -> ProviderAccount:
        """Atomically commit credentials and this account's discovery snapshot."""

        owner = _owner(owner)
        account_id = _required(account_id, "account_id")
        connection_id = _required(connection_id, "connection_id")
        normalized_status = _required(status, "status").lower()
        if normalized_status not in {"complete", "unavailable", "partial", "reauth_required"}:
            raise ProviderValidationError("unsupported account discovery status")
        if normalized_status == "complete" and not authoritative:
            raise ProviderValidationError("complete account discovery must be authoritative")
        normalized_acquisition_method = _provider_acquisition_method(acquisition_method)
        normalized: list[dict[str, Any]] = []
        seen_models: set[str] = set()
        for raw in model_routes:
            value = dict(raw)
            model_id = _required(value.get("provider_model_id"), "provider_model_id")
            if model_id in seen_models:
                raise ProviderValidationError("engine model routes contain a duplicate model")
            seen_models.add(model_id)
            operations = sorted({_required(item, "operation") for item in value.get("operations") or ()})
            if not operations:
                raise ProviderValidationError("engine model route has no operations")
            normalized.append({
                "id": _required(value.get("id"), "model_route_id"),
                "provider_model_id": model_id,
                "display_name": _required(value.get("display_name") or model_id, "display_name"),
                "operations": operations,
                "capabilities": _json_mapping(value.get("capabilities")),
                "provenance": _json_mapping(value.get("provenance")),
                "enabled": bool(value.get("enabled", True)),
            })
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            connection = self._require_connection(db, owner, connection_id)
            account = db.query(ProviderAccount).filter(
                ProviderAccount.id == account_id,
                ProviderAccount.owner == owner,
            ).first()
            creating = account is None
            already_applied = False
            if creating:
                if expected_account_revision not in (None, 0):
                    raise ProviderRevisionConflict(expected=expected_account_revision, current=0)
                fingerprint = credential_fingerprint(
                    credentials, owner=owner, connection_id=connection.id
                )
                duplicate = db.query(ProviderAccount.id).filter(
                    ProviderAccount.connection_id == connection.id,
                    ProviderAccount.credential_fingerprint == fingerprint,
                    ProviderAccount.deleted_at.is_(None),
                ).first()
                if duplicate is not None:
                    raise ProviderConflict("credential is already registered for this connection")
                last_order = db.query(func.max(ProviderAccount.sort_order)).filter(
                    ProviderAccount.connection_id == connection.id,
                    ProviderAccount.deleted_at.is_(None),
                ).scalar()
                account = ProviderAccount(
                    id=account_id,
                    connection_id=connection.id,
                    owner=owner,
                    label=_required(label, "label"),
                    auth_method=_required(auth_method, "auth_method"),
                    acquisition_method=normalized_acquisition_method,
                    auth_class=_required(auth_class, "auth_class"),
                    sort_order=(last_order if last_order is not None else -1) + 1,
                    enabled=True,
                    credential_envelope=seal_credential(
                        credentials,
                        CredentialScope(owner, connection.id, account_id, int(expected_credential_revision)),
                    ),
                    credential_fingerprint=fingerprint,
                    credential_version=int(expected_credential_revision),
                    safe_identity=_json_mapping(safe_identity),
                )
                db.add(account)
                connection.revision += 1
            else:
                if account.connection_id != connection.id or account.deleted_at is not None:
                    raise ProviderNotFound("provider account was not found")
                incoming_fingerprint = credential_fingerprint(
                    credentials, owner=owner, connection_id=connection.id
                )
                already_applied = (
                    expected_account_revision is None
                    and int(account.credential_version) == int(expected_credential_revision)
                    and account.credential_fingerprint == incoming_fingerprint
                )
                if not already_applied:
                    if expected_account_revision is None:
                        raise ProviderValidationError("expected account revision is required")
                    account = self._claim_row_revision(
                        db, ProviderAccount,
                        (ProviderAccount.id == account_id, ProviderAccount.owner == owner,
                         ProviderAccount.deleted_at.is_(None)),
                        expected_revision=expected_account_revision,
                        missing_message="provider account was not found",
                    )
                    if int(account.credential_version) + 1 != int(expected_credential_revision):
                        raise ProviderRevisionConflict(
                            expected=expected_credential_revision,
                            current=int(account.credential_version),
                        )
                    account = self._install_credential(
                        db, account, credentials,
                        expected_credential_version=int(account.credential_version),
                        bump_revision=False,
                    )
                    account.label = _required(label, "label")
                    account.auth_method = _required(auth_method, "auth_method")
                    if normalized_acquisition_method is not None:
                        account.acquisition_method = normalized_acquisition_method
                    account.auth_class = _required(auth_class, "auth_class")
                    account.safe_identity = _json_mapping(safe_identity)
                    account.enabled = True
                    connection.revision += 1

            db.flush()
            current_rows = db.query(ProviderModelRoute).filter(
                ProviderModelRoute.owner == owner,
                ProviderModelRoute.connection_id == connection.id,
            ).all()
            current = {row.provider_model_id: row for row in current_rows}
            catalog_revision = max((int(row.catalog_revision or 0) for row in current_rows), default=0) + 1
            active_rows: list[ProviderModelRoute] = []
            for value in normalized:
                row = current.get(value["provider_model_id"])
                if row is None:
                    row = ProviderModelRoute(
                        id=value["id"], connection_id=connection.id, owner=owner,
                        provider_model_id=value["provider_model_id"],
                    )
                    db.add(row)
                else:
                    if bool(row.enabled) != value["enabled"] or row.deleted_at is not None or list(row.operations or ()) != value["operations"]:
                        self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=row.provider_model_id)
                    row.revision += 1
                row.display_name = value["display_name"]
                row.operations = value["operations"]
                row.capabilities = value["capabilities"]
                row.provenance = _merge_route_provenance(value["provenance"], provenance)
                row.catalog_revision = catalog_revision
                row.enabled = value["enabled"]
                row.deleted_at = None
                active_rows.append(row)
            if normalized_status == "complete":
                db.flush()
                active_ids = {row.id for row in active_rows if row.enabled}
                existing = {row.model_route_id: row for row in db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id == account.id,
                ).all()}
                evidence = {"authority": "managed-engine", "discovery_status": normalized_status}
                evidence.update(_json_mapping(provenance))
                for route in {route.id: route for route in (*current_rows, *active_rows)}.values():
                    entitlement = existing.get(route.id)
                    if entitlement is None:
                        entitlement = ProviderAccountEntitlement(account_id=account.id, model_route_id=route.id)
                        db.add(entitlement)
                    else:
                        entitlement.revision += 1
                    if bool(entitlement.eligible) != (route.id in active_ids):
                        self._invalidate_entitlement_lease(db,owner=owner,account=account,route_id=route.id)
                    entitlement.eligible = route.id in active_ids
                    entitlement.evidence = evidence
                    entitlement.last_seen_at = timestamp
                for route in current_rows:
                    if route.id in active_ids:
                        continue
                    sibling = db.query(ProviderAccountEntitlement).join(
                        ProviderAccount, ProviderAccount.id == ProviderAccountEntitlement.account_id,
                    ).filter(
                        ProviderAccountEntitlement.model_route_id == route.id,
                        ProviderAccountEntitlement.eligible.is_(True),
                        ProviderAccountEntitlement.account_id != account.id,
                        ProviderAccount.connection_id == connection.id,
                        ProviderAccount.deleted_at.is_(None),
                    ).first()
                    if sibling is None and route.deleted_at is None:
                        self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,model_id=route.provider_model_id)
                        route.enabled = False
                        route.deleted_at = timestamp
                        route.revision += 1
            health = db.query(ProviderAccountHealth).filter(
                ProviderAccountHealth.account_id == account.id,
            ).first()
            if health is None:
                health = ProviderAccountHealth(account_id=account.id)
                db.add(health)
            else:
                health.revision += 1
            health.state = {"complete": "healthy", "unavailable": "degraded", "partial": "degraded", "reauth_required": "reauth_required"}[normalized_status]
            health.last_error_code = _safe_error_code(error_code)
            if normalized_status == "complete":
                health.last_success_at = timestamp
            db.flush()
            return self._detach(db, account)

    def permitted_model_route_ids(
        self,
        *,
        owner: str,
        connection_id: str,
        allowed_account_ids: Optional[Iterable[str]] = None,
    ) -> set[str]:
        """Return route IDs eligible for at least one live account."""

        owner = _owner(owner)
        connection_id = _required(connection_id, "connection_id")
        allowed = (
            {_required(item, "account_id") for item in allowed_account_ids}
            if allowed_account_ids is not None
            else None
        )
        with self._transaction() as db:
            connection = self._require_connection(db, owner, connection_id)
            accounts = db.query(ProviderAccount.id).filter(
                ProviderAccount.owner == owner,
                ProviderAccount.connection_id == connection_id,
                ProviderAccount.enabled.is_(True),
                ProviderAccount.deleted_at.is_(None),
            )
            if allowed is not None:
                if not allowed:
                    return set()
                accounts = accounts.filter(ProviderAccount.id.in_(allowed))
            account_ids = [row[0] for row in accounts.all()]
            if not account_ids:
                if str(connection.kind or "").strip().lower() not in _KEYLESS_CONNECTION_KINDS:
                    return set()
                return {
                    row[0]
                    for row in db.query(ProviderModelRoute.id).filter(
                        ProviderModelRoute.connection_id == connection_id,
                        ProviderModelRoute.enabled.is_(True),
                        ProviderModelRoute.deleted_at.is_(None),
                    ).all()
                }
            rows = db.query(ProviderAccountEntitlement.model_route_id).filter(
                ProviderAccountEntitlement.account_id.in_(account_ids),
                ProviderAccountEntitlement.eligible.is_(True),
            ).distinct().all()
            if not rows:
                # Legacy/keyless rows may have accounts created before the
                # entitlement snapshot existed.  Once any snapshot exists,
                # the explicit eligible flags are authoritative.
                has_snapshot = db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                ).first() is not None
                if not has_snapshot:
                    if str(connection.kind or "").strip().lower() not in _KEYLESS_CONNECTION_KINDS:
                        return set()
                    return {
                        row[0]
                        for row in db.query(ProviderModelRoute.id).filter(
                            ProviderModelRoute.connection_id == connection_id,
                            ProviderModelRoute.enabled.is_(True),
                            ProviderModelRoute.deleted_at.is_(None),
                        ).all()
                    }
            return {str(row[0]) for row in rows}

    def route_has_permitted_account(
        self,
        *,
        owner: str,
        model_route_id: str,
        allowed_account_ids: Optional[Iterable[str]] = None,
    ) -> bool:
        route_id = _required(model_route_id, "model_route_id")
        with self._transaction() as db:
            normalized_owner = _owner(owner)
            route = self._require_route(db, normalized_owner, route_id)
            connection = self._require_connection(db, normalized_owner, route.connection_id)
            account_query = db.query(ProviderAccount.id).filter(
                ProviderAccount.owner == _owner(owner),
                ProviderAccount.connection_id == route.connection_id,
                ProviderAccount.enabled.is_(True),
                ProviderAccount.deleted_at.is_(None),
            )
            if allowed_account_ids is not None:
                account_ids = {
                    _required(item, "account_id") for item in allowed_account_ids
                }
                # An empty all-live selector means the owner currently has no
                # account rows; retain keyless/share catalogue compatibility.
                if account_ids:
                    account_query = account_query.filter(ProviderAccount.id.in_(account_ids))
            account_ids = [row[0] for row in account_query.all()]
            if not account_ids:
                return str(connection.kind or "").strip().lower() in _KEYLESS_CONNECTION_KINDS
            entitlements = db.query(ProviderAccountEntitlement).filter(
                ProviderAccountEntitlement.account_id.in_(account_ids),
                ProviderAccountEntitlement.model_route_id == route.id,
            ).all()
            if not entitlements:
                # Preserve pre-snapshot account rows; explicit false is a
                # revocation and therefore remains a hard denial.
                if db.query(ProviderAccountEntitlement).filter(
                    ProviderAccountEntitlement.account_id.in_(account_ids),
                ).first() is not None:
                    return False
                return str(connection.kind or "").strip().lower() in _KEYLESS_CONNECTION_KINDS
            for entitlement in entitlements:
                if not entitlement.eligible:
                    continue
                health = db.query(ProviderAccountHealth).filter(
                    ProviderAccountHealth.account_id == entitlement.account_id,
                ).first()
                if health is not None and not self._health_available(
                    health.state,
                    health.cooldown_until,
                    quota_reset_at=health.quota_reset_at,
                    now=_utc(self._clock()),
                ):
                    continue
                model_health = db.query(ProviderAccountModelHealth).filter(
                    ProviderAccountModelHealth.account_id == entitlement.account_id,
                    ProviderAccountModelHealth.model_route_id == route.id,
                ).first()
                if model_health is None or self._health_available(
                    model_health.state,
                    model_health.cooldown_until,
                    quota_reset_at=model_health.quota_reset_at,
                    now=_utc(self._clock()),
                    model_scope=True,
                ):
                    return True
            return False

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
                    if not row.eligible:
                        self._invalidate_entitlement_lease(db,owner=owner,account=account,route_id=route.id)
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
                if bool(row.eligible) != bool(eligible):
                    self._invalidate_credential_leases(db,owner=owner,connection_id=account.connection_id,account_id=account.id,model_id=route.provider_model_id)
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
        scope_kind: str = "auxiliary_operation",
        operation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        execution_revision: Optional[int] = None,
        connection_id: str,
        provider_id: Optional[str] = None,
        model_id: Optional[str] = None,
        model_route_id: Optional[str] = None,
        preferred_account_id: Optional[str] = None,
        inherited_account_id: Optional[str] = None,
        grant_id: Optional[str] = None,
        allowed_account_ids: Optional[Iterable[str]] = None,
        billing_lane: Optional[str] = None,
        method_policy: Optional[Mapping[str, Any]] = None,
        private_authority: Optional[Mapping[str, Any]] = None,
        trusted_options: Optional[Mapping[str, Any]] = None,
        trusted_logging_policy: Optional[Mapping[str, Any]] = None,
        _fresh=None,
        now: Optional[datetime] = None,
    ) -> AccountSelection:
        owner = _owner(owner)
        root_operation_id = _required(root_operation_id, "root_operation_id")
        connection_id = _required(connection_id, "connection_id")
        if scope_kind not in {"actor_execution", "auxiliary_operation"}:
            raise ProviderValidationError("unsupported provider binding scope kind")
        trusted_logging_policy = _validate_logging_policy_snapshot(trusted_logging_policy)
        if scope_kind != 'actor_execution' and trusted_logging_policy is not None:
            raise ProviderValidationError('logging policy snapshot requires actor execution')
        if scope_kind == "auxiliary_operation" and any(
            value is not None for value in (session_id, actor_id, execution_revision)
        ):
            raise ProviderValidationError("auxiliary binding cannot claim actor identity")
        operation_id = _required(operation_id, "operation_id") if operation_id is not None else None
        if scope_kind == "auxiliary_operation" and operation_id is None:
            # Direct in-process legacy callers retain their historical path;
            # the managed wire callback separately requires its journal ID.
            pass
        if scope_kind == "actor_execution" and operation_id is not None:
            raise ProviderValidationError("actor execution binding cannot claim an auxiliary operation")
        actor_scope_key, session_id, actor_id, execution_revision, model_route_id = _actor_execution_scope(
            session_id=session_id,
            actor_id=actor_id,
            execution_revision=execution_revision,
            model_route_id=model_route_id,
        )
        if scope_kind == "actor_execution" and actor_scope_key == "legacy":
            raise ProviderValidationError("actor execution binding requires its exact identity")
        if scope_kind == "auxiliary_operation":
            actor_scope_key = "legacy"
        selection_identity = {
            "scope_kind": scope_kind,
            "operation_id": operation_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "execution_revision": execution_revision,
            "model_route_id": model_route_id,
        }
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
        if _fresh is not None:
            from src.openclank.provider_callbacks import _FreshMainBinding
            if type(_fresh) is not _FreshMainBinding:
                raise ProviderValidationError('fresh main authority unavailable')
            _fresh.assert_current()
            request = json.loads(_fresh.request_json)
            if (owner != _fresh.admission.owner or root_operation_id != _fresh.admission.root_id
                    or scope_kind != 'actor_execution' or actor_id != 'main' or session_id != _fresh.admission.session_id
                    or request.get('executionRevision') != execution_revision or request.get('modelRouteID') != model_route_id
                    or request.get('connectionID') != connection_id or request.get('modelID') != model_id
                    or request.get('providerID') != provider_id or request.get('billingLane') != billing_lane
                    or request.get('grantID') is not None or request.get('modelRequestOptions') != trusted_options
                    or request.get('preferredAccountID') != preferred_account_id
                    or request.get('inheritedAccountID') != inherited_account_id
                    or request.get('methodPolicy') != method_policy or allowed is not None
                    or private_authority is not None):
                raise ProviderValidationError('fresh main selection changed')
        with (self._fresh_binding_transaction(_fresh) if _fresh is not None else self._transaction()) as db:
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
            claude_subscription = connection.family_id == "anthropic" and connection.billing_lane == "subscription"
            if claude_subscription:
                if scope_kind != "actor_execution":
                    raise ProviderValidationError("Claude subscription auxiliary operations are unavailable")
                _check_method_policy(method_policy, credential_owner)
            elif method_policy is not None:
                raise ProviderValidationError("provider method policy is not applicable")
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
            persisted_route_id = model_route_id
            if scope_kind == "auxiliary_operation":
                if operation_id is not None:
                    if resolved_route is None:
                        raise ProviderValidationError("auxiliary operation requires an exact model route")
                    from core.operation_models import OperationJournal

                    journal = db.query(OperationJournal).filter(
                        OperationJournal.id == operation_id,
                        OperationJournal.owner == owner,
                        OperationJournal.root_operation_id == root_operation_id,
                    ).first()
                    if (
                        journal is None
                        or journal.state in {"complete", "failed", "cancelled"}
                        or journal.connection_id != connection.id
                        or journal.billing_lane != requested_lane
                        or journal.model_route_id != resolved_route.id
                        or journal.operation not in set(resolved_route.operations or ())
                    ):
                        raise ShareDenied("auxiliary provider binding escaped its admitted operation route")
                    persisted_route_id = resolved_route.id

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

            self._reject_enrolled_scope(db, owner, root_operation_id, actor_scope_key)
            binding = (
                db.query(ProviderOperationBinding)
                .filter(
                    ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.root_operation_id == root_operation_id,
                    ProviderOperationBinding.actor_scope_key == actor_scope_key,
                    ProviderOperationBinding.connection_id == connection.id,
                    ProviderOperationBinding.billing_lane == requested_lane,
                    ProviderOperationBinding.model_id == wire_model_id,
                )
                .first()
            )
            fresh_policy = None
            if _fresh is not None:
                from src.openclank.provider_cross_model import profile_ready
                selection = _fresh.selection
                selection.assert_current()
                if selection._admission._store is not self:
                    raise ProviderValidationError('captured provider store changed')
                from core.database import Session as DbSession
                chat = db.query(DbSession.owner, DbSession.provider_model_route_id).filter(DbSession.id == selection.chat_id).first()
                if chat is None or chat.owner != owner or chat.provider_model_route_id != selection.route_id:
                    raise ProviderValidationError('captured provider chat changed')
                if not profile_ready(db.connection()) or resolved_route is None:
                    raise ProviderValidationError('captured provider policy unavailable')
                fresh_policy = self._cross_model_policy(db.connection(), persisted_route_id)
                encode = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if (fresh_policy is None or not fresh_policy['enabled'] or encode(fresh_policy) != selection.policy_json
                        or selection.route_id != resolved_route.id or selection.route_revision != resolved_route.revision
                        or selection.connection_id != connection.id or selection.connection_revision != connection.revision
                        or selection.model_id != resolved_route.provider_model_id
                        or selection.catalog_revision != resolved_route.catalog_revision
                        or selection.candidate_json != _fresh.admission.option_context_json):
                    raise ProviderValidationError('captured provider policy changed')
                if fresh_policy and db.query(ProviderOperationBinding.id).filter(
                        ProviderOperationBinding.owner == owner,
                        ProviderOperationBinding.root_operation_id == root_operation_id,
                        ).first() is not None:
                    raise ProviderConflict('fresh main binding already exists')
            if binding is not None:
                if (binding.policy_snapshot or {}).get("private_authority") != private_authority:
                    raise ProviderConflict("provider binding private authority changed")
                if (binding.policy_snapshot or {}).get("main_model_options") != trusted_options:
                    raise ProviderConflict("provider binding model option proof changed")
                if claude_subscription and _binding_method_policy(binding) != method_policy:
                    raise ProviderConflict("provider binding policy proof changed")
                if (
                    binding.credential_owner != credential_owner
                    or binding.connection_id != connection.id
                    or binding.provider_id != wire_provider_id
                    or binding.billing_lane != requested_lane
                    or binding.model_id != wire_model_id
                    or binding.model_route_id != persisted_route_id
                    or binding.operation_id != operation_id
                ):
                    raise ProviderConflict(
                        "actor execution is already bound to a different provider route"
                    )
                if binding.grant_id != grant_id:
                    raise ProviderConflict("actor execution is already bound under a different grant")
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
                        method_policy=_binding_method_policy(binding),
                        **selection_identity,
                    )
                account = self._require_account(
                    db,
                    binding.credential_owner,
                    binding.selected_account_id,
                )
                if not account.enabled:
                    raise NoEligibleAccount("sticky provider account is disabled")
                if account.connection_id != connection.id:
                    raise NoEligibleAccount("sticky provider binding is invalid")
                if claude_subscription:
                    _check_method_policy(method_policy, credential_owner, account)
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
                    method_policy=_binding_method_policy(binding),
                    **selection_identity,
                )

            pool_policy = _account_pool_policy(connection.account_pool_policy)
            if pool_policy is not None and pool_policy.get("account_id"):
                target = db.query(ProviderAccount.id).filter(
                    ProviderAccount.id == pool_policy["account_id"],
                    ProviderAccount.owner == credential_owner,
                    ProviderAccount.connection_id == connection.id,
                ).first()
                if target is None:
                    raise ProviderConflict("account pool policy references an invalid account")

            # Existing bindings above remain sticky. For a new root, the
            # configured pin/preference is authoritative; inherited account
            # stickiness remains ahead of a preferred policy but not a pin.
            resolved_inherited_id = inherited_account_id
            resolved_preferred_id = preferred_account_id
            if pool_policy is not None:
                mode = str(pool_policy["mode"])
                if mode == "pinned":
                    resolved_inherited_id = None
                    resolved_preferred_id = str(pool_policy["account_id"])
                elif inherited_account_id:
                    resolved_preferred_id = inherited_account_id
                elif mode == "preferred":
                    resolved_preferred_id = str(pool_policy["account_id"])
                else:
                    resolved_preferred_id = None

            candidates = self._eligible_account_rows(
                db,
                owner=credential_owner,
                connection=connection,
                model_route_id=(resolved_route.id if resolved_route else None),
                allowed_account_ids=allowed,
                now=timestamp,
            )
            if not candidates:
                if pool_policy is not None and pool_policy.get("account_id"):
                    raise NoEligibleAccount(
                        "configured provider account is unavailable for this model"
                    )
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
                        policy_snapshot={**({"private_authority": dict(private_authority)} if private_authority else {}),
                        **({"main_model_options": dict(trusted_options)} if trusted_options is not None else {}),
                        **({_LOGGING_POLICY_SNAPSHOT_KEY: trusted_logging_policy} if trusted_logging_policy is not None else {})},
                        actor_scope_key=actor_scope_key,
                        operation_id=operation_id,
                        engine_session_id=session_id,
                        actor_id=actor_id,
                        execution_revision=execution_revision,
                        model_route_id=persisted_route_id,
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
                        method_policy=_binding_method_policy(binding),
                        **selection_identity,
                    )
                raise NoEligibleAccount(
                    "no eligible account exists in this connection and billing lane"
                )

            selected: Optional[ProviderAccount] = None
            used_preference = False
            fallback_reason: Optional[str] = None
            source = "round_robin"
            if pool_policy is not None and pool_policy["mode"] == "pinned":
                selected = next(
                    (row for row in candidates if row.id == pool_policy["account_id"]),
                    None,
                )
                if selected is None:
                    raise NoEligibleAccount(
                        "pinned provider account is unavailable for this model"
                    )
                source = "pinned"
            elif resolved_inherited_id:
                selected = next(
                    (row for row in candidates if row.id == resolved_inherited_id),
                    None,
                )
                if selected is None:
                    raise NoEligibleAccount(
                        "inherited account is not eligible for the requested model"
                    )
                source = "inherited"
            elif resolved_preferred_id:
                selected = next(
                    (row for row in candidates if row.id == resolved_preferred_id),
                    None,
                )
                used_preference = selected is not None
                if selected is not None:
                    source = "preferred"
                else:
                    if pool_policy is not None and not pool_policy["same_model_fallback"]:
                        raise NoEligibleAccount(
                            "preferred provider account is unavailable and fallback is disabled"
                        )
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

            if claude_subscription:
                _check_method_policy(method_policy, credential_owner, selected)
            binding = ProviderOperationBinding(
                id=f"pbd_{uuid.uuid4().hex}",
                owner=owner,
                credential_owner=credential_owner,
                root_operation_id=root_operation_id,
                actor_scope_key=actor_scope_key,
                operation_id=operation_id,
                engine_session_id=session_id,
                actor_id=actor_id,
                execution_revision=execution_revision,
                model_route_id=persisted_route_id,
                connection_id=connection.id,
                provider_id=wire_provider_id,
                billing_lane=requested_lane,
                model_id=wire_model_id,
                grant_id=grant_id,
                selected_account_id=selected.id,
                credential_revision=selected.credential_version,
                preferred_account_id=resolved_preferred_id,
                source=source,
                attempt=1,
                fallback_reason=fallback_reason,
                policy_snapshot={**_account_pool_policy_snapshot(
                    connection=connection,
                    policy=pool_policy,
                    candidates=candidates,
                ), **({_METHOD_POLICY_SNAPSHOT_KEY: dict(method_policy)} if claude_subscription else {}),
                    **({"private_authority": dict(private_authority)} if private_authority else {}),
                    **({"main_model_options": dict(trusted_options)} if trusted_options is not None else {}),
                    **({_LOGGING_POLICY_SNAPSHOT_KEY: trusted_logging_policy} if trusted_logging_policy is not None else {})},
                revision=1,
            )
            db.add(binding)
            if fresh_policy is not None:
                # Only the selected binding exists in this still-uncommitted writer
                # transaction. No credential has been disclosed on this path.
                db.flush()
                _fresh.assert_current()
                if trusted_options is not None:
                    from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
                    current_options = validate_request_option_selection(route_id=resolved_route.id,
                        catalog_revision=resolved_route.catalog_revision,
                        descriptors=project_request_options(resolved_route.capabilities, catalog_revision=resolved_route.catalog_revision, source='engine-catalog'),
                        selection={'route_id':trusted_options.get('optionRouteID'), 'catalog_revision':trusted_options.get('catalogRevision'), 'options':trusted_options.get('options')})
                    if current_options != trusted_options.get('options'):
                        raise ProviderValidationError('fresh main options changed')
                alternatives = self._cross_model_alternatives(db, _fresh.admission, binding, fresh_policy)
                if json.dumps(alternatives, sort_keys=True, separators=(',', ':'), allow_nan=False) != _fresh.selection.alternatives_json:
                    raise ProviderValidationError('captured provider alternatives changed')
                root = _fresh.admission
                coordinator = {'version':1, 'revision':1, 'state':'provisional',
                    'original':{'owner':root.owner, 'account_id':root.account_id, 'chat_id':root.chat_id,
                        'session_id':root.session_id, 'root_id':root.root_id, 'holder_id':root.holder_id,
                        'generation':root.generation, 'owner_epoch':root.owner_epoch,
                        'root_nonce':root._root.provider_root_nonce,
                        'actor_id':'main', 'execution_revision':execution_revision,
                        'options_sha256':hashlib.sha256(root.option_context_json.encode('utf-8')).hexdigest()},
                    'primary':self._cross_model_primary(db,binding), 'policy':fresh_policy,
                    'alternatives':alternatives, 'active_index':0}
                encoded = json.dumps(coordinator, sort_keys=True, separators=(',', ':'))
                from src.openclank.provider_cross_model import set_writer
                sql_connection = db.connection()
                def authorize(op, bid, principal, root_id, actor, old, new):
                    try:
                        _fresh.assert_current()
                        return int(not _fresh.callback._generation_closed.is_set() and
                            (op,bid,principal,root_id,actor,old,new) ==
                            ('update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,None,encoded))
                    except Exception: return 0
                set_writer(sql_connection, authorize)
                try:
                    changed = sql_connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator IS NULL', (encoded,binding.id,binding.revision))
                    if changed.rowcount != 1: raise ProviderConflict('fresh main binding changed')
                finally:
                    try: set_writer(sql_connection)
                    except Exception:
                        sql_connection.invalidate()
                        raise
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
                method_policy=_binding_method_policy(binding),
                **selection_identity,
            )

    def mark_binding_committed(
        self,
        *,
        owner: str,
        root_operation_id: str,
        connection_id: str,
        billing_lane: str,
        session_id: Optional[str] = None,
        actor_id: Optional[str] = None,
        execution_revision: Optional[int] = None,
        model_route_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> ProviderOperationBinding:
        owner = _owner(owner)
        actor_scope_key, _, _, _, _ = _actor_execution_scope(
            session_id=session_id,
            actor_id=actor_id,
            execution_revision=execution_revision,
            model_route_id=model_route_id,
        )
        with self._transaction() as db:
            self._reject_enrolled_scope(db, owner, _required(root_operation_id, 'root_operation_id'), actor_scope_key)
            row = (
                db.query(ProviderOperationBinding)
                .filter(
                    ProviderOperationBinding.owner == owner,
                    ProviderOperationBinding.root_operation_id
                    == _required(root_operation_id, "root_operation_id"),
                    ProviderOperationBinding.actor_scope_key == actor_scope_key,
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
            scope_kind=("actor_execution" if row.actor_id is not None else "auxiliary_operation"),
            operation_id=row.operation_id,
            session_id=row.engine_session_id,
            actor_id=row.actor_id,
            execution_revision=row.execution_revision,
            model_route_id=row.model_route_id,
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
            method_policy=_binding_method_policy(row),
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
            self._reject_enrolled(db, row)
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
        evidence: Optional[Mapping[str, Any]] = None,
        dispatch_observation: Optional[Mapping[str, Any]] = None,
        refresh_continuity: Optional[Mapping[str, Any]] = None,
        trusted_refresh_check=None,
        success_continuity: Optional[Mapping[str, Any]] = None,
        trusted_success_check=None,
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
            "invalid_output",
            "auth",
            "quota",
            "entitlement",
            "transient",
            "unknown",
        }
        if normalized_outcome not in allowed_outcomes:
            raise ProviderValidationError("unsupported provider attempt outcome")
        evidence = _validated_attempt_evidence(evidence, normalized_outcome)
        if refresh_continuity is not None or normalized_outcome in {'success','invalid_output'}:
            try: dispatch_observation = _validated_dispatch_observation(dispatch_observation, normalized_outcome, evidence)
            except ProviderValidationError: dispatch_observation = None
        else:
            dispatch_observation = _validated_dispatch_observation(dispatch_observation, normalized_outcome, evidence)
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
            existing = db.query(ProviderOperationBinding).filter_by(id=binding_id, owner=owner).first()
            if existing is not None:
                self._reject_enrolled(db, existing)
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
            if normalized_outcome == "invalid_output" and row.state != "committed":
                raise ProviderValidationError("semantic output outcome requires a committed attempt")
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

            if evidence is not None and 'providerReset' in evidence:
                observed = datetime.fromisoformat(evidence['providerReset']['observedAt'][:-1] + '+00:00')
                if (row.provider_id != 'anthropic' or connection.family_id != 'anthropic'
                    or connection.kind != 'official' or (connection.normalized_url is not None
                        and connection.normalized_url.rstrip('/') not in {'https://api.anthropic.com','https://api.anthropic.com/v1'})
                    or row.billing_lane != 'metered_api' or selected is None
                    or selected.auth_class != 'metered' or selected.auth_method != 'api_key'
                    or abs(_utc(observed) - timestamp) > timedelta(minutes=5)):
                    raise ProviderValidationError('provider request-rate evidence is unavailable for this binding')

            codex_subscription = (
                connection.family_id == 'openai' and connection.adapter_id == 'openai-responses'
                and connection.kind in {'official', 'subscription'} and connection.billing_lane == 'subscription'
            )
            if refresh_continuity is not None:
                continuity=refresh_continuity
                try:
                    valid=(type(continuity) is dict and set(continuity)=={'version','admissionID','previousCredentialRevision','credentialRevision'}
                        and type(continuity['version']) is int and continuity['version']==1
                        and type(continuity['admissionID']) is str and 0<len(continuity['admissionID'])<=256
                        and type(continuity['previousCredentialRevision']) is int and continuity['previousCredentialRevision']>=1
                        and type(continuity['credentialRevision']) is int and continuity['credentialRevision']==continuity['previousCredentialRevision']+1
                        and dispatch_observation is not None and dispatch_observation['profile']=='openai.codex.responses.v1'
                        and row.state=='uncommitted' and row.credential_revision==continuity['credentialRevision']
                        and selected is not None and selected.credential_version==continuity['credentialRevision']
                        and row.provider_id=='openai' and codex_subscription
                        and row.billing_lane=='subscription' and selected.auth_class=='subscription' and selected.auth_method=='oauth'
                        and callable(trusted_refresh_check) and trusted_refresh_check(db,row,continuity,dispatch_observation) is True)
                except Exception:
                    valid=False
                if not valid: dispatch_observation=None

            if normalized_outcome in {'success','invalid_output'}:
                continuity=success_continuity
                try:
                    valid=(type(continuity) is dict and type(continuity.get('version')) is int
                        and ((continuity['version']==1 and set(continuity)=={'version','admissionID','credentialRevision'})
                             or (continuity['version']==2 and set(continuity)=={'version','admissionID','credentialRevision','previousCredentialRevision'}
                                 and type(continuity['previousCredentialRevision']) is int and continuity['previousCredentialRevision']>=1
                                 and continuity['credentialRevision']==continuity['previousCredentialRevision']+1))
                        and type(continuity['admissionID']) is str and 0<len(continuity['admissionID'])<=256
                        and type(continuity['credentialRevision']) is int and continuity['credentialRevision']>=1
                        and dispatch_observation is not None and dispatch_observation['profile']=='openai.codex.responses.v1'
                        and row.state=='committed' and row.credential_revision==continuity['credentialRevision']
                        and selected is not None and selected.credential_version==continuity['credentialRevision']
                        and row.provider_id=='openai' and codex_subscription
                        and row.billing_lane=='subscription' and selected.auth_class=='subscription' and selected.auth_method=='oauth'
                        and callable(trusted_success_check) and trusted_success_check(db,row,continuity,dispatch_observation) is True)
                except Exception: valid=False
                if not valid: dispatch_observation=None

            if dispatch_observation is not None:
                profile=dispatch_observation['profile']
                if (selected is None
                    or (profile=='anthropic.messages.v1' and (connection.kind!='official' or row.provider_id!='anthropic' or connection.family_id!='anthropic'
                        or row.billing_lane!='metered_api' or selected.auth_class!='metered' or selected.auth_method!='api_key'
                        or (connection.normalized_url is not None and connection.normalized_url.rstrip('/') not in {'https://api.anthropic.com','https://api.anthropic.com/v1'})))
                    or (profile=='openai.codex.responses.v1' and (row.provider_id!='openai' or not codex_subscription
                        or row.billing_lane!='subscription' or selected.auth_class!='subscription' or selected.auth_method!='oauth'))):
                    raise ProviderValidationError('provider dispatch observation is unavailable for this binding')

            if evidence is not None and 'codexWindows' in evidence:
                observed = datetime.fromisoformat(evidence['codexWindows']['observedAt'][:-1] + '+00:00')
                if (row.provider_id != 'openai' or not codex_subscription
                    or row.billing_lane != 'subscription' or selected is None
                    or selected.auth_class != 'subscription' or selected.auth_method != 'oauth'
                    or abs(_utc(observed) - timestamp) > timedelta(minutes=5)):
                    raise ProviderValidationError('Codex window evidence is unavailable for this binding')

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
            if evidence is not None:
                attempt_entry["evidence"] = evidence
            if dispatch_observation is not None:
                attempt_entry['dispatch_observation'] = dispatch_observation
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
                    # A local cooldown is not a provider-observed reset.
                    health.quota_reset_at = None
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
                snapshot = row.policy_snapshot
                if snapshot is not None:
                    if (
                        not isinstance(snapshot, Mapping)
                        or snapshot.get("version") != 1
                        or snapshot.get("source") not in {"legacy", "explicit"}
                        or type(snapshot.get("same_model_fallback")) is not bool
                        or not isinstance(snapshot.get("ordered_account_ids"), list)
                    ):
                        raise ProviderConflict("provider binding policy snapshot is invalid")
                    if not snapshot["same_model_fallback"] or snapshot.get("mode") == "pinned":
                        candidates = []
                    else:
                        by_id = {candidate.id: candidate for candidate in candidates}
                        ordered_ids = snapshot["ordered_account_ids"]
                        if (
                            len(ordered_ids) > _MAX_POLICY_SNAPSHOT_ACCOUNTS
                            or any(not isinstance(item, str) for item in ordered_ids)
                            or len(set(ordered_ids)) != len(ordered_ids)
                        ):
                            raise ProviderConflict("provider binding policy candidate list is invalid")
                        candidates = [
                            by_id[account_id]
                            for account_id in ordered_ids
                            if account_id in by_id
                        ]
                if not candidates:
                    row.fallback_reason = (
                        f"{normalized_outcome}_fallback_disabled"
                        if snapshot is not None and not snapshot.get("same_model_fallback")
                        else f"{normalized_outcome}_all_accounts_exhausted"
                    )
                    exhausted = True
                else:
                    next_account = candidates[0]
                    if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                        _check_method_policy(_binding_method_policy(row), row.credential_owner, next_account)
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

    def _prepare_credential_lease(self, *, binding, account, owner, holder_id, timestamp, ttl_seconds):
        """Construct the same scoped lease and grant for both owned transactions."""
        credentials = unseal_credential(account.credential_envelope,
            CredentialScope(account.owner, account.connection_id, account.id, account.credential_version))
        lease_id = 'pcl_' + secrets.token_urlsafe(32)
        expires_at = timestamp + timedelta(seconds=ttl_seconds)
        row = ProviderCredentialLease(lease_id_digest=self._credential_lease_digest(lease_id),
            owner=owner, credential_owner=binding.credential_owner, holder_id=holder_id,
            root_operation_id=binding.root_operation_id, binding_id=binding.id,
            connection_id=binding.connection_id, account_id=account.id, model_id=binding.model_id,
            grant_id=binding.grant_id, credential_revision=account.credential_version,
            issued_at=timestamp, expires_at=expires_at)
        grant = CredentialLeaseGrant(lease_id=lease_id, connection_id=binding.connection_id,
            account_id=account.id, credential_revision=account.credential_version, expires_at=expires_at,
            credentials=credentials, binding_id=binding.id, session_id=binding.engine_session_id,
            actor_id=binding.actor_id, execution_revision=binding.execution_revision,
            root_operation_id=binding.root_operation_id, model_route_id=binding.model_route_id,
            scope_kind=('actor_execution' if binding.actor_id is not None else 'auxiliary_operation'),
            operation_id=binding.operation_id, method_policy=_binding_method_policy(binding))
        return row, grant

    def issue_first_cross_model_credential(self, admission, callback_authority, primary_binding_id,
                                           expected_binding_revision, expected_credential_revision,
                                           expected_coordinator_revision, *, ttl_seconds=30):
        """Private first issue; a committed attempt is never adopted or repeated."""
        if (type(expected_binding_revision) is not int or expected_binding_revision < 1
                or type(expected_credential_revision) is not int or expected_credential_revision < 1
                or type(expected_coordinator_revision) is not int or expected_coordinator_revision not in (1, 2)):
            raise ProviderValidationError('cross-model issue revision unavailable')
        try: ttl_seconds = float(ttl_seconds)
        except (ValueError, TypeError, OverflowError): raise ProviderValidationError('credential lease TTL unavailable') from None
        if not 0 < ttl_seconds <= 60: raise ProviderValidationError('credential lease TTL unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            binding, value, old, admitted = self._prepare_cross_model_admission(
                db, connection, admission, primary_binding_id, expected_binding_revision)
            if expected_coordinator_revision != (2 if admitted else 1):
                raise ProviderConflict('cross-model coordinator changed')
            if (binding.grant_id is not None or binding.credential_owner != admission.owner
                    or binding.state != 'uncommitted' or not admission._root.provider_root_nonce):
                raise ProviderValidationError('cross-model issue unavailable')
            if db.query(ProviderCredentialLease.lease_id_digest).filter_by(binding_id=binding.id).first() is not None:
                raise ProviderConflict('cross-model prior credential state unavailable')
            account = self._current_credential_authority(db, binding, expected_credential_revision)
            timestamp = _utc(self._clock())
            lease, grant = self._prepare_credential_lease(binding=binding, account=account,
                owner=admission.owner, holder_id=admission.holder_id, timestamp=timestamp, ttl_seconds=ttl_seconds)
            route = self._require_route(db, binding.credential_owner, binding.model_route_id)
            next_revision = 3 if admitted else 2
            attempt = {'version':1, 'state':'may_have_issued', 'nonce':secrets.token_hex(32), 'generation':1,
                'coordinator_revision':next_revision, 'original':self._cross_model_original(admission),
                'primary':copy.deepcopy(value['primary']),
                'binding_id':binding.id, 'binding_revision':binding.revision, 'credential_revision':account.credential_version,
                'route_id':route.id, 'route_revision':route.revision, 'catalog_revision':route.catalog_revision,
                'options_sha256':hashlib.sha256(admission.option_context_json.encode('utf-8')).hexdigest(),
                'lease_digest':lease.lease_id_digest}
            value['version'] = 2; value['revision'] = next_revision; value['active_attempt'] = attempt
            new = json.dumps(value, sort_keys=True, separators=(',', ':'))
            from src.openclank.provider_cross_model import set_writer
            phase = [0]
            lease_identity = json.dumps([lease.lease_id_digest,lease.credential_owner,lease.holder_id,lease.connection_id,
                lease.account_id,lease.model_id,lease.grant_id,lease.credential_revision], ensure_ascii=False, separators=(',', ':'))
            def authorize(op, bid, owner, root_id, actor, prior, replacement):
                try:
                    admission.assert_current()
                    if callback_authority._generation_closed.is_set() or admission._callback is not callback_authority or callback_authority._store is not self:
                        return 0
                    if phase[0] == 0 and (op,bid,owner,root_id,actor,prior,replacement) == (
                            'update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,old,new):
                        phase[0] = 1; return 1
                    if phase[0] == 1 and (op,bid,owner,root_id,actor,prior,replacement) == (
                            'lease-insert',binding.id,binding.owner,binding.root_operation_id,admission.holder_id,None,lease_identity
                            ) and lease.lease_id_digest == attempt['lease_digest'] and lease.holder_id == admission.holder_id:
                        phase[0] = 2; return 1
                except Exception: pass
                return 0
            set_writer(connection, authorize)
            try:
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                    (new,binding.id,expected_binding_revision,old))
                if changed.rowcount != 1 or phase[0] != 1: raise ProviderConflict('cross-model issue changed')
                db.add(lease); db.flush([lease])
                if phase[0] != 2: raise ProviderConflict('cross-model lease unavailable')
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            identity = {'version':1, 'coordinatorRevision':next_revision, 'attemptGeneration':1,
                        'attemptNonce':attempt['nonce']}
        # The context committed the may-have-issued fact before the grant escapes.
        admission.assert_current()
        if callback_authority._generation_closed.is_set():
            raise ProviderValidationError('cross-model issue outcome unavailable')
        return grant, identity

    def issue_explicit_cross_model_alternative(self, admission, callback_authority, primary_binding_id,
                                               expected_binding_revision, expected_coordinator_revision,
                                               expected_attempt_id, *, expected_credential_revision, ttl_seconds=30):
        """Issue one private H2 continuation under the original F2 authority."""
        if (type(expected_binding_revision) is not int or expected_binding_revision < 2
            or type(expected_coordinator_revision) is not int or expected_coordinator_revision < 2
            or type(expected_attempt_id) is not str or re.fullmatch(r'[0-9a-f]{64}', expected_attempt_id) is None
            or type(expected_credential_revision) is not int or expected_credential_revision < 1):
            raise ProviderValidationError('alternative authority unavailable')
        try: ttl_seconds = float(ttl_seconds)
        except (TypeError, ValueError, OverflowError): raise ProviderValidationError('alternative lease unavailable') from None
        if not 0 < ttl_seconds <= 60: raise ProviderValidationError('alternative lease unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            if admission._root is None or admission._attachment is None or not admission._root.provider_root_nonce:
                raise ProviderValidationError('original provider root unavailable')
            primary = self._cross_model_binding(db, admission, primary_binding_id)
            self._require_revision(primary.revision, expected_binding_revision)
            if primary.credential_revision != expected_credential_revision:
                raise ProviderValidationError('primary credential changed')
            old = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (primary.id,)).scalar_one()
            try:
                if type(old) is not str or len(old.encode('utf-8')) > 65536: raise ValueError()
                state = json.loads(old)
            except (TypeError, ValueError, UnicodeError):
                raise ProviderValidationError('alternative coordinator unavailable') from None
            keys = {'version','revision','state','original','primary','policy','alternatives','active_index','active_attempt'}
            attempt_keys = {'version','state','nonce','generation','coordinator_revision','original','primary','binding_id',
                'binding_revision','credential_revision','route_id','route_revision','catalog_revision','options_sha256','lease_digest','terminal'}
            if (type(state) is not dict or set(state) != keys or type(state['version']) is not int or state['version'] != 3
                or type(state['revision']) is not int or state['revision'] != expected_coordinator_revision
                or state['state'] != 'auth_no_output_closed' or type(state['active_index']) is not int or state['active_index'] != 0
                or state['original'] != self._cross_model_original(admission) or type(state['primary']) is not dict
                or type(state['active_attempt']) is not dict or set(state['active_attempt']) != attempt_keys):
                raise ProviderValidationError('alternative coordinator unavailable')
            active = state['active_attempt']
            if (type(active['version']) is not int or active['version'] != 1 or active['state'] != 'may_have_issued' or active['nonce'] != expected_attempt_id
                or type(active['generation']) is not int or active['generation'] != 1 or active['coordinator_revision'] != state['revision']
                or active['original'] != state['original'] or active['primary'] != state['primary']
                or active['binding_id'] != primary.id or active['binding_revision'] != state['primary'].get('binding_revision')
                or active['credential_revision'] != primary.credential_revision or active['route_id'] != primary.model_route_id
                or active['route_revision'] != state['primary'].get('route_revision')
                or active['catalog_revision'] != state['primary'].get('catalog_revision')
                or active['options_sha256'] != hashlib.sha256(admission.option_context_json.encode('utf-8')).hexdigest()
                or type(active['lease_digest']) is not str or re.fullmatch(r'[0-9a-f]{64}', active['lease_digest']) is None
                or type(active['terminal']) is not dict or type(active['terminal'].get('version')) is not int
                or type(active['terminal'].get('attempt_generation')) is not int
                or active['terminal'] != {'version':1,'profile':'anthropic.messages.401-auth-no-output.v1',
                    'attempt_nonce':expected_attempt_id,'attempt_generation':1}
                or primary.state != 'uncommitted' or primary.grant_id is not None or primary.credential_owner != admission.owner):
                raise ProviderValidationError('alternative terminal unavailable')
            roots = db.query(ProviderOperationBinding).filter_by(owner=admission.owner, root_operation_id=admission.root_id).limit(2).all()
            if len(roots) != 1 or roots[0].id != primary.id:
                raise ProviderValidationError('alternative root unavailable')
            current_primary = self._cross_model_primary(db, primary)
            frozen_primary = state['primary']
            if (type(frozen_primary) is not dict or set(frozen_primary) != set(current_primary)
                or type(frozen_primary['binding_revision']) is not int or primary.revision != frozen_primary['binding_revision'] + 1
                or {k:v for k,v in current_primary.items() if k != 'binding_revision'} != {k:v for k,v in frozen_primary.items() if k != 'binding_revision'}):
                raise ProviderValidationError('alternative primary changed')
            from src.openclank.provider_cross_model import validate_policy, set_writer
            policy = validate_policy(state['policy'])
            if (not policy['enabled'] or len(policy['alternatives']) != 1
                or self._cross_model_policy(connection, primary.model_route_id) != policy
                or self._cross_model_alternatives(db, admission, primary, policy) != state['alternatives']):
                raise ProviderValidationError('alternative policy changed')
            original_connection = self._require_connection(db, admission.owner, primary.connection_id)
            def official(conn):
                return (conn.enabled and conn.deleted_at is None and conn.family_id == 'anthropic'
                    and conn.adapter_id == 'anthropic-messages' and conn.kind == 'official'
                    and conn.billing_lane == 'metered_api' and (conn.normalized_url is None
                        or conn.normalized_url.rstrip('/') in {'https://api.anthropic.com','https://api.anthropic.com/v1'}))
            if not official(original_connection): raise ProviderValidationError('alternative provider unavailable')
            if len(db.query(ProviderAccount.id).filter_by(owner=admission.owner, connection_id=original_connection.id).limit(1025).all()) > 1024:
                raise ProviderValidationError('primary account inventory unavailable')
            original_accounts = self._eligible_account_rows(db, owner=admission.owner, connection=original_connection,
                model_route_id=primary.model_route_id, allowed_account_ids=None, now=_utc(self._clock()))
            if len(original_accounts) != 1 or original_accounts[0].id != primary.selected_account_id:
                raise ProviderValidationError('primary account inventory unavailable')
            original_account = original_accounts[0]
            leases = db.query(ProviderCredentialLease).filter_by(owner=admission.owner, root_operation_id=admission.root_id).limit(2).all()
            if len(leases) != 1:
                raise ProviderValidationError('primary lease inventory unavailable')
            prior = leases[0]
            if (prior.lease_id_digest != active['lease_digest'] or prior.binding_id != primary.id
                or prior.credential_owner != admission.owner or prior.holder_id != admission.holder_id
                or prior.connection_id != primary.connection_id or prior.account_id != original_account.id
                or prior.model_id != primary.model_id or prior.grant_id is not None
                or prior.credential_revision != primary.credential_revision):
                raise ProviderValidationError('primary lease changed')
            alt = state['alternatives'][0]
            route = self._require_route(db, admission.owner, alt['route_id'])
            target = self._require_connection(db, admission.owner, alt['connection_id'])
            if (alt['grant_id'] is not None or route.owner != admission.owner or not official(target)
                or route.connection_id != target.id or 'chat.stream' not in (route.operations or ())):
                raise ProviderValidationError('alternative route unavailable')
            candidates = self._eligible_account_rows(db, owner=admission.owner, connection=target,
                model_route_id=route.id, allowed_account_ids=None, now=_utc(self._clock()))
            if len(candidates) != 1: raise ProviderValidationError('alternative account inventory unavailable')
            selected = candidates[0]
            for account in (original_account, selected):
                if (account.auth_method != 'api_key' or account.auth_class != 'metered'
                    or account.acquisition_method != 'manual-key' or not account.credential_envelope):
                    raise ProviderValidationError('alternative credential unavailable')
            if selected.id == original_account.id: raise ProviderValidationError('alternative credential unavailable')
            first_secret = unseal_credential(original_account.credential_envelope,
                CredentialScope(original_account.owner, original_account.connection_id, original_account.id, original_account.credential_version))
            second_secret = unseal_credential(selected.credential_envelope,
                CredentialScope(selected.owner, selected.connection_id, selected.id, selected.credential_version))
            first_key, second_key = first_secret.get('key'), second_secret.get('key')
            if (first_secret.get('type') != 'api' or second_secret.get('type') != 'api'
                or not isinstance(first_key, str) or not first_key or not isinstance(second_key, str) or not second_key
                or hmac.compare_digest(first_key.encode('utf-8'), second_key.encode('utf-8'))):
                raise ProviderValidationError('alternative credential unavailable')
            del first_secret, second_secret, first_key, second_key
            now = _utc(self._clock())
            policy_snapshot = {**_account_pool_policy_snapshot(connection=target, policy={'mode':'pinned','account_id':selected.id,'same_model_fallback':False}, candidates=candidates),
                'alternative_options':copy.deepcopy(alt['options'])}
            fields = {'id':'pbd_'+uuid.uuid4().hex, 'owner':admission.owner, 'credential_owner':admission.owner,
                'root_operation_id':primary.root_operation_id, 'actor_scope_key':primary.actor_scope_key,
                'engine_session_id':primary.engine_session_id, 'actor_id':primary.actor_id, 'execution_revision':primary.execution_revision,
                'operation_id':None, 'connection_id':target.id, 'provider_id':target.family_id, 'billing_lane':target.billing_lane,
                'model_id':route.provider_model_id, 'model_route_id':route.id, 'grant_id':None,
                'selected_account_id':selected.id, 'credential_revision':selected.credential_version, 'preferred_account_id':None,
                'source':'failover', 'attempt':1, 'state':'uncommitted', 'committed_at':None,
                'fallback_reason':None, 'attempts':[], 'policy_snapshot':policy_snapshot, 'revision':1}
            binding = ProviderOperationBinding(**fields, created_at=now, updated_at=now)
            lease, grant = self._prepare_credential_lease(binding=binding, account=selected, owner=admission.owner,
                holder_id=admission.holder_id, timestamp=now, ttl_seconds=ttl_seconds)
            next_revision = state['revision'] + 1
            alternative_attempt = {'version':1,'state':'may_have_issued','nonce':secrets.token_hex(32),'generation':2,
                'coordinator_revision':next_revision,'original':copy.deepcopy(state['original']), 'route':copy.deepcopy(alt),
                'binding':copy.deepcopy(fields),'lease_digest':lease.lease_id_digest,
                'account_revision':selected.revision,'primary_account_revision':original_account.revision}
            if alternative_attempt['nonce'] == expected_attempt_id:
                raise ProviderValidationError('alternative attempt unavailable')
            state['version']=4; state['state']='alternative_may_have_issued'; state['revision']=next_revision
            state['active_index']=1; state['alternative_attempt']=alternative_attempt
            new = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
            if len(new.encode('utf-8')) > 65536: raise ProviderValidationError('alternative coordinator unavailable')
            lease_tuple = [lease.lease_id_digest,lease.credential_owner,lease.holder_id,lease.connection_id,
                lease.account_id,lease.model_id,lease.grant_id,lease.credential_revision]
            lease_json = json.dumps(lease_tuple, ensure_ascii=False, separators=(',', ':'))
            phase = [0]
            def authorize(op, bid, owner, root, actor, before, after):
                try:
                    admission.assert_current()
                    if callback_authority._generation_closed.is_set() or admission._callback is not callback_authority or callback_authority._store is not self: return 0
                    expected = [('insert',binding.id,primary.owner,primary.root_operation_id,primary.actor_scope_key,None,None),
                        ('lease-insert',binding.id,primary.owner,primary.root_operation_id,admission.holder_id,None,lease_json),
                        ('update',primary.id,primary.owner,primary.root_operation_id,primary.actor_scope_key,old,new)]
                    if phase[0] < len(expected) and (op,bid,owner,root,actor,before,after) == expected[phase[0]]:
                        phase[0] += 1; return 1
                except Exception: pass
                return 0
            set_writer(connection, authorize)
            try:
                db.add(binding); db.flush([binding])
                if phase[0] != 1: raise ProviderConflict('alternative binding unavailable')
                persisted = db.query(ProviderOperationBinding).filter_by(id=binding.id).populate_existing().one()
                if (any(getattr(persisted, key) != value for key,value in fields.items())
                    or persisted.created_at != now or persisted.updated_at != now
                    or connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (binding.id,)).scalar_one() is not None
                    or self._current_credential_authority(db, persisted, selected.credential_version).id != selected.id
                    or db.query(ProviderAccount).filter_by(id=selected.id, owner=admission.owner).populate_existing().one().revision != alternative_attempt['account_revision']
                    or db.query(ProviderAccount).filter_by(id=original_account.id, owner=admission.owner).populate_existing().one().revision != alternative_attempt['primary_account_revision']):
                    raise ProviderConflict('alternative binding changed')
                db.add(lease); db.flush([lease])
                if phase[0] != 2: raise ProviderConflict('alternative lease unavailable')
                stored = db.query(ProviderCredentialLease).filter_by(lease_id_digest=lease.lease_id_digest).populate_existing().one()
                if [stored.lease_id_digest,stored.credential_owner,stored.holder_id,stored.connection_id,stored.account_id,stored.model_id,stored.grant_id,stored.credential_revision] != lease_tuple or stored.owner != admission.owner or stored.root_operation_id != admission.root_id or stored.binding_id != binding.id or stored.issued_at != lease.issued_at or stored.expires_at != lease.expires_at:
                    raise ProviderConflict('alternative lease changed')
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?', (new,primary.id,expected_binding_revision,old))
                if changed.rowcount != 1 or phase[0] != 3: raise ProviderConflict('alternative coordinator changed')
                if connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (primary.id,)).scalar_one() != new:
                    raise ProviderConflict('alternative coordinator changed')
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            receipt = {'version':1,'primaryBindingID':primary.id,'primaryBindingRevision':primary.revision,
                'coordinatorRevision':next_revision,'alternativeBindingID':binding.id,'alternativeBindingRevision':binding.revision,
                'attemptID':alternative_attempt['nonce'],'attemptGeneration':2}
            selection = self._selection_from_binding(db, binding, sticky=False).to_wire()
            route_result = copy.deepcopy(alt)
        admission.assert_current()
        if callback_authority._generation_closed.is_set(): raise ProviderValidationError('alternative issue outcome unavailable')
        return grant, receipt, selection, route_result

    def _read_issued_cross_model_alternative(self, db, connection, admission, primary_binding_id,
                                             expected_primary_revision, expected_coordinator_revision,
                                             alternative_binding_id, expected_alternative_revision, attempt_id):
        primary = self._cross_model_binding_identity(db, admission, primary_binding_id)
        self._require_revision(primary.revision, expected_primary_revision)
        old = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (primary.id,)).scalar_one()
        try:
            if type(old) is not str or len(old.encode('utf-8')) > 65536: raise ValueError()
            state = json.loads(old)
        except (ValueError, TypeError, UnicodeError):
            raise ProviderValidationError('alternative coordinator unavailable') from None
        keys = {'version','revision','state','original','primary','policy','alternatives','active_index','active_attempt','alternative_attempt'}
        if (type(state) is not dict or set(state) != keys or type(state['version']) is not int or state['version'] != 4
            or type(state['revision']) is not int or state['revision'] != expected_coordinator_revision
            or state['state'] != 'alternative_may_have_issued' or type(state['active_index']) is not int or state['active_index'] != 1
            or state['original'] != self._cross_model_original(admission) or primary.state != 'uncommitted'):
            raise ProviderValidationError('alternative coordinator unavailable')
        alternative = state['alternative_attempt']; h2 = state['active_attempt']
        alt_keys = {'version','state','nonce','generation','coordinator_revision','original','route','binding','lease_digest','account_revision','primary_account_revision'}
        if (type(alternative) is not dict or set(alternative) != alt_keys
            or type(alternative['version']) is not int or alternative['version'] != 1
            or alternative['state'] != 'may_have_issued' or alternative['nonce'] != attempt_id
            or type(alternative['generation']) is not int or alternative['generation'] != 2
            or alternative['coordinator_revision'] != state['revision'] or alternative['original'] != state['original']
            or type(state['alternatives']) is not list or len(state['alternatives']) != 1
            or alternative['route'] != state['alternatives'][0] or type(alternative['binding']) is not dict
            or type(alternative['account_revision']) is not int or alternative['account_revision'] < 1
            or type(alternative['primary_account_revision']) is not int or alternative['primary_account_revision'] < 1
            or alternative['binding'].get('id') != alternative_binding_id
            or alternative['binding'].get('revision') != expected_alternative_revision
            or type(alternative['lease_digest']) is not str or re.fullmatch(r'[0-9a-f]{64}', alternative['lease_digest']) is None
            or type(h2) is not dict or type(h2.get('terminal')) is not dict
            or h2['terminal'].get('profile') != 'anthropic.messages.401-auth-no-output.v1'
            or h2['terminal'].get('attempt_nonce') != h2.get('nonce')
            or h2['terminal'].get('attempt_generation') != 1):
            raise ProviderValidationError('alternative attempt changed')
        h2_keys = {'version','state','nonce','generation','coordinator_revision','original','primary','binding_id',
            'binding_revision','credential_revision','route_id','route_revision','catalog_revision','options_sha256','lease_digest','terminal'}
        from src.openclank.provider_cross_model import validate_policy
        policy = validate_policy(state['policy'])
        primary_snapshot = state['primary']
        route_snapshot = alternative['route']
        route_keys = {'route_id','route_revision','connection_id','connection_revision','provider_id','model_id','lane','grant_id','grant_revision','options'}
        if (set(h2) != h2_keys or type(h2['version']) is not int or h2['version'] != 1
            or h2['state'] != 'may_have_issued' or type(h2['generation']) is not int or h2['generation'] != 1
            or type(h2['nonce']) is not str or re.fullmatch(r'[0-9a-f]{64}', h2['nonce']) is None or h2['nonce'] == attempt_id
            or type(h2['coordinator_revision']) is not int or h2['coordinator_revision'] + 1 != state['revision']
            or h2['original'] != state['original'] or h2['primary'] != primary_snapshot
            or type(primary_snapshot) is not dict or h2['binding_id'] != primary.id
            or h2['binding_revision'] != primary_snapshot.get('binding_revision')
            or primary.revision != primary_snapshot.get('binding_revision', 0) + 1
            or h2['credential_revision'] != primary.credential_revision or h2['route_id'] != primary.model_route_id
            or h2['route_revision'] != primary_snapshot.get('route_revision') or h2['catalog_revision'] != primary_snapshot.get('catalog_revision')
            or h2['options_sha256'] != hashlib.sha256(admission.option_context_json.encode('utf-8')).hexdigest()
            or type(h2['lease_digest']) is not str or re.fullmatch(r'[0-9a-f]{64}', h2['lease_digest']) is None
            or type(h2['terminal'].get('version')) is not int or type(h2['terminal'].get('attempt_generation')) is not int
            or h2['terminal'] != {'version':1,'profile':'anthropic.messages.401-auth-no-output.v1',
                'attempt_nonce':h2['nonce'],'attempt_generation':1}
            or not policy['enabled'] or len(policy['alternatives']) != 1
            or type(route_snapshot) is not dict or set(route_snapshot) != route_keys
            or route_snapshot['route_id'] != policy['alternatives'][0]['route_id']
            or route_snapshot['options'] != policy['alternatives'][0]['options']):
            raise ProviderValidationError('alternative terminal changed')
        fields = alternative['binding']
        expected_fields = {'id','owner','credential_owner','root_operation_id','actor_scope_key','engine_session_id','actor_id',
            'execution_revision','operation_id','connection_id','provider_id','billing_lane','model_id','model_route_id','grant_id',
            'selected_account_id','credential_revision','preferred_account_id','source','attempt','state','committed_at',
            'fallback_reason','attempts','policy_snapshot','revision'}
        rows = db.query(ProviderOperationBinding).filter_by(owner=admission.owner, root_operation_id=admission.root_id).limit(3).all()
        if set(fields) != expected_fields or len(rows) != 2 or {row.id for row in rows} != {primary.id, alternative_binding_id}:
            raise ProviderValidationError('alternative root changed')
        binding = next(row for row in rows if row.id == alternative_binding_id)
        if (any(getattr(binding,key) != value for key,value in fields.items())
            or binding.actor_scope_key != primary.actor_scope_key or binding.engine_session_id != admission.session_id
            or binding.actor_id != 'main' or binding.execution_revision != admission.execution_revision
            or binding.owner != admission.owner or binding.credential_owner != admission.owner
            or binding.grant_id is not None or binding.state != 'uncommitted'
            or binding.connection_id != route_snapshot['connection_id'] or binding.provider_id != route_snapshot['provider_id']
            or binding.billing_lane != route_snapshot['lane'] or binding.model_id != route_snapshot['model_id']
            or binding.model_route_id != route_snapshot['route_id']
            or (binding.policy_snapshot or {}).get('alternative_options') != route_snapshot['options']
            or connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?',(binding.id,)).scalar_one() is not None):
            raise ProviderValidationError('alternative binding changed')
        leases = db.query(ProviderCredentialLease).filter_by(owner=admission.owner, root_operation_id=admission.root_id).limit(3).all()
        if len(leases) != 2 or {row.lease_id_digest for row in leases} != {h2['lease_digest'], alternative['lease_digest']}:
            raise ProviderValidationError('alternative lease inventory changed')
        prior_lease = next(row for row in leases if row.lease_id_digest == h2['lease_digest'])
        if (prior_lease.binding_id != primary.id or prior_lease.credential_owner != admission.owner
            or prior_lease.holder_id != admission.holder_id or prior_lease.connection_id != primary.connection_id
            or prior_lease.account_id != primary.selected_account_id or prior_lease.model_id != primary.model_id
            or prior_lease.grant_id is not None or prior_lease.credential_revision != primary.credential_revision):
            raise ProviderValidationError('primary lease changed')
        lease = next(row for row in leases if row.lease_id_digest == alternative['lease_digest'])
        if (lease is None or lease.owner != admission.owner or lease.credential_owner != admission.owner
            or lease.root_operation_id != admission.root_id or lease.binding_id != binding.id
            or lease.holder_id != admission.holder_id or lease.connection_id != binding.connection_id
            or lease.account_id != binding.selected_account_id or lease.model_id != binding.model_id
            or lease.grant_id is not None or lease.credential_revision != binding.credential_revision):
            raise ProviderValidationError('alternative lease changed')
        return primary, binding, state, old, alternative, lease

    def check_explicit_cross_model_dispatch(self, admission, callback_authority, primary_binding_id,
                                            expected_primary_revision, expected_coordinator_revision,
                                            alternative_binding_id, expected_alternative_revision, attempt_id):
        """Point-in-time current authority; never issues, commits or advances."""
        if (type(expected_primary_revision) is not int or expected_primary_revision < 2
            or type(expected_alternative_revision) is not int or expected_alternative_revision != 1
            or type(expected_coordinator_revision) is not int or expected_coordinator_revision < 3
            or type(attempt_id) is not str or re.fullmatch(r'[0-9a-f]{64}', attempt_id) is None):
            raise ProviderValidationError('alternative dispatch unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            primary, binding, state, _old, alternative, lease = self._read_issued_cross_model_alternative(
                db, connection, admission, primary_binding_id, expected_primary_revision, expected_coordinator_revision,
                alternative_binding_id, expected_alternative_revision, attempt_id)
            checked = self._cross_model_binding(db, admission, primary.id)
            current_primary = self._cross_model_primary(db, checked)
            frozen = state['primary']
            if ({k:v for k,v in current_primary.items() if k != 'binding_revision'} !=
                {k:v for k,v in frozen.items() if k != 'binding_revision'}):
                raise ProviderValidationError('primary authority changed')
            if (self._cross_model_policy(connection, primary.model_route_id) != state['policy']
                or self._cross_model_alternatives(db, admission, primary, state['policy']) != state['alternatives']):
                raise ProviderValidationError('alternative authority changed')
            target = self._require_connection(db, admission.owner, binding.connection_id)
            route = self._require_route(db, admission.owner, binding.model_route_id)
            if (target.family_id != 'anthropic' or target.adapter_id != 'anthropic-messages' or target.kind != 'official'
                or not target.enabled or target.billing_lane != 'metered_api' or target.deleted_at is not None
                or (target.normalized_url is not None and target.normalized_url.rstrip('/') not in {'https://api.anthropic.com','https://api.anthropic.com/v1'})
                or not route.enabled or route.deleted_at is not None or 'chat.stream' not in (route.operations or ())):
                raise ProviderValidationError('alternative authority changed')
            original_connection = self._require_connection(db, admission.owner, primary.connection_id)
            if (len(db.query(ProviderAccount.id).filter_by(owner=admission.owner, connection_id=original_connection.id).limit(1025).all()) > 1024
                or len(db.query(ProviderAccount.id).filter_by(owner=admission.owner, connection_id=target.id).limit(1025).all()) > 1024):
                raise ProviderValidationError('alternative account inventory unavailable')
            primary_accounts = self._eligible_account_rows(db, owner=admission.owner, connection=original_connection,
                model_route_id=primary.model_route_id, allowed_account_ids=None, now=_utc(self._clock()))
            target_accounts = self._eligible_account_rows(db, owner=admission.owner, connection=target,
                model_route_id=binding.model_route_id, allowed_account_ids=None, now=_utc(self._clock()))
            if (len(primary_accounts) != 1 or primary_accounts[0].id != primary.selected_account_id
                or len(target_accounts) != 1 or target_accounts[0].id != binding.selected_account_id):
                raise ProviderValidationError('alternative account inventory changed')
            primary_account = self._current_credential_authority(db, primary, primary.credential_revision)
            selected = self._current_credential_authority(db, binding, binding.credential_revision)
            if (primary_account.revision != alternative['primary_account_revision']
                or selected.revision != alternative['account_revision'] or _utc(lease.expires_at) <= _utc(self._clock())):
                raise ProviderValidationError('alternative authority changed')
            return {'version':1,'state':'ready','primaryBindingID':primary.id,'primaryBindingRevision':primary.revision,
                'coordinatorRevision':state['revision'],'alternativeBindingID':binding.id,
                'alternativeBindingRevision':binding.revision,'attemptID':attempt_id,'attemptGeneration':2}

    def commit_explicit_cross_model_alternative(self, admission, callback_authority, primary_binding_id,
                                                expected_primary_revision, expected_coordinator_revision,
                                                alternative_binding_id, expected_alternative_revision, attempt_id):
        """Private positive stop for the exact issued alternate; never advances."""
        if (type(expected_primary_revision) is not int or expected_primary_revision < 2
            or type(expected_alternative_revision) is not int or expected_alternative_revision != 1
            or type(expected_coordinator_revision) is not int or expected_coordinator_revision < 3
            or type(attempt_id) is not str or re.fullmatch(r'[0-9a-f]{64}', attempt_id) is None):
            raise ProviderValidationError('alternative commitment unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            primary, binding, state, old, alternative, lease = self._read_issued_cross_model_alternative(
                db, connection, admission, primary_binding_id, expected_primary_revision, expected_coordinator_revision,
                alternative_binding_id, expected_alternative_revision, attempt_id)
            fields = alternative['binding']
            state['state'] = 'alternative_committed'; state['revision'] += 1
            alternative['state'] = 'committed'; alternative['coordinator_revision'] = state['revision']
            new = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False)
            from src.openclank.provider_cross_model import set_writer
            phase = [0]
            def authorize(op, bid, owner, root, actor, before, after):
                try:
                    admission.assert_current()
                    if callback_authority._generation_closed.is_set() or admission._callback is not callback_authority or callback_authority._store is not self: return 0
                    expected = [('update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,None,None),
                        ('update',primary.id,primary.owner,primary.root_operation_id,primary.actor_scope_key,old,new)]
                    if phase[0] < len(expected) and (op,bid,owner,root,actor,before,after) == expected[phase[0]]:
                        phase[0] += 1; return 1
                except Exception: pass
                return 0
            set_writer(connection, authorize)
            try:
                now = _utc(self._clock())
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET state=?, committed_at=?, revision=? WHERE id=? AND revision=? AND state=?',
                    ('committed',now,expected_alternative_revision+1,binding.id,expected_alternative_revision,'uncommitted'))
                if changed.rowcount != 1 or phase[0] != 1: raise ProviderConflict('alternative commitment changed')
                db.expire_all()
                checked = db.query(ProviderOperationBinding).filter_by(id=binding.id).one()
                expected = dict(fields); expected.update(state='committed', committed_at=now, revision=expected_alternative_revision+1)
                if any(getattr(checked,key) != value for key,value in expected.items()):
                    raise ProviderConflict('alternative commitment changed')
                changed = connection.exec_driver_sql('UPDATE provider_operation_bindings SET cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                    (new,primary.id,expected_primary_revision,old))
                if changed.rowcount != 1 or phase[0] != 2: raise ProviderConflict('alternative coordinator changed')
                if connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?',(primary.id,)).scalar_one() != new:
                    raise ProviderConflict('alternative coordinator changed')
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            receipt = {'version':1,'state':'committed','primaryBindingID':primary.id,
                'primaryBindingRevision':expected_primary_revision,'coordinatorRevision':state['revision'],
                'alternativeBindingID':binding.id,'alternativeBindingRevision':expected_alternative_revision+1,
                'attemptID':attempt_id,'attemptGeneration':2}
        admission.assert_current()
        if callback_authority._generation_closed.is_set(): raise ProviderValidationError('alternative commitment outcome unavailable')
        return receipt

    def control_cross_model_attempt(self, admission, callback_authority, binding_id, expected_binding_revision,
                                    attempt_id, attempt_generation, expected_coordinator_revision,
                                    *, outcome=None, account_id=None, terminal_profile=None):
        """Record one conservative stop under the original F2 attempt; no rotation."""
        if (type(expected_binding_revision) is not int or expected_binding_revision < 1
                or type(expected_coordinator_revision) is not int or expected_coordinator_revision < 2
                or type(attempt_generation) is not int or attempt_generation != 1
                or type(attempt_id) is not str or len(attempt_id) != 64
                or any(c not in '0123456789abcdef' for c in attempt_id)
                or outcome not in (None, 'success','auth','quota','entitlement','transient','unknown')
                or terminal_profile not in (None, 'anthropic.messages.401-auth-no-output.v1')
                or (terminal_profile is not None and (outcome is not None or account_id is not None))):
            raise ProviderValidationError('cross-model control unavailable')
        with self._cross_model_transaction(admission, callback_authority) as (db, connection):
            if admission._root is None or admission._attachment is None or not admission._root.provider_root_nonce:
                raise ProviderValidationError('original provider root unavailable')
            binding = self._cross_model_binding_identity(db, admission, binding_id)
            self._require_revision(binding.revision, expected_binding_revision)
            old = connection.exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=?', (binding.id,)).scalar_one()
            try:
                if type(old) is not str or len(old.encode('utf-8')) > 65536: raise ValueError()
                value = json.loads(old)
            except (ValueError, TypeError, UnicodeError):
                raise ProviderValidationError('cross-model coordinator unavailable') from None
            keys = {'version','revision','state','original','primary','policy','alternatives','active_index','active_attempt'}
            attempt_keys = {'version','state','nonce','generation','coordinator_revision','original','primary','binding_id',
                'binding_revision','credential_revision','route_id','route_revision','catalog_revision','options_sha256','lease_digest'}
            if (type(value) is not dict or set(value) != keys or type(value['version']) is not int or value['version'] != 2
                    or type(value['revision']) is not int or value['revision'] != expected_coordinator_revision
                    or value['state'] not in ('admitted','committed','unknown') or type(value['active_index']) is not int or value['active_index'] != 0
                    or value['original'] != self._cross_model_original(admission) or type(value['active_attempt']) is not dict
                    or type(value['primary']) is not dict):
                raise ProviderValidationError('cross-model coordinator unavailable')
            active = value['active_attempt']
            primary_keys = {'binding_id','binding_revision','route_id','route_revision','catalog_revision','connection_id','connection_revision',
                'model_id','lane','account_id','credential_revision','grant_id','grant_revision','options'}
            last = active.get('last_control')
            if (set(active) not in (attempt_keys, attempt_keys | {'last_control'}) or type(active.get('version')) is not int or active['version'] != 1
                    or active.get('state') != 'may_have_issued' or active.get('nonce') != attempt_id
                    or type(active.get('generation')) is not int or active['generation'] != attempt_generation
                    or type(active.get('coordinator_revision')) is not int or active['coordinator_revision'] != value['revision']
                    or active.get('original') != value['original'] or active.get('primary') != value['primary']
                    or active.get('binding_id') != binding.id or active.get('credential_revision') != binding.credential_revision
                    or active.get('route_id') != binding.model_route_id or active.get('options_sha256') != hashlib.sha256(admission.option_context_json.encode('utf-8')).hexdigest()
                    or active.get('binding_revision') != value['primary'].get('binding_revision')
                    or active.get('route_revision') != value['primary'].get('route_revision')
                    or active.get('catalog_revision') != value['primary'].get('catalog_revision')
                    or set(value['primary']) != primary_keys or value['primary'].get('binding_id') != binding.id
                    or value['primary'].get('route_id') != binding.model_route_id
                    or value['primary'].get('credential_revision') != binding.credential_revision
                    or ('last_control' in active and (type(last) is not dict or set(last) != {'outcome','accounting'}
                        or last['outcome'] not in ('success','auth','quota','entitlement','transient','unknown') or last['accounting'] != 'unavailable'))
                    or type(active.get('lease_digest')) is not str or len(active['lease_digest']) != 64
                    or any(c not in '0123456789abcdef' for c in active['lease_digest'])
                    or binding.grant_id is not None or binding.credential_owner != admission.owner
                    or value['primary'].get('account_id') != binding.selected_account_id
                    or value['primary'].get('connection_id') != binding.connection_id
                    or value['primary'].get('model_id') != binding.model_id
                    or value['primary'].get('lane') != binding.billing_lane
                    or (outcome is not None and account_id != binding.selected_account_id)
                    or (account_id is not None and account_id != binding.selected_account_id)):
                raise ProviderValidationError('cross-model attempt changed')
            if terminal_profile is not None:
                provider = db.query(ProviderConnection).filter_by(id=binding.connection_id, owner=binding.credential_owner).first()
                if (value['state'] != 'admitted' or binding.state != 'uncommitted' or 'last_control' in active
                        or provider is None or provider.family_id != 'anthropic' or binding.provider_id != provider.family_id or provider.billing_lane != 'metered_api'
                        or binding.billing_lane != 'metered_api' or value['primary']['lane'] != 'metered_api'):
                    raise ProviderValidationError('cross-model terminal unavailable')
                active['terminal'] = {'version':1, 'profile':terminal_profile, 'attempt_nonce':attempt_id,
                                      'attempt_generation':attempt_generation}
                value['version'] = 3; value['state'] = 'auth_no_output_closed'; value['revision'] += 1
                active['coordinator_revision'] = value['revision']
                new = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
                if len(new.encode('utf-8')) > 65536: raise ProviderValidationError('cross-model terminal unavailable')
                from src.openclank.provider_cross_model import set_writer
                def authorize_terminal(op, bid, owner, root, actor, prior, replacement):
                    try:
                        admission.assert_current()
                        return int(not callback_authority._generation_closed.is_set() and admission._callback is callback_authority
                            and callback_authority._store is self and (op,bid,owner,root,actor,prior,replacement) ==
                            ('update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,old,new))
                    except Exception: return 0
                set_writer(connection, authorize_terminal)
                try:
                    changed=connection.exec_driver_sql('UPDATE provider_operation_bindings SET revision=?, cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                        (binding.revision+1,new,binding.id,expected_binding_revision,old))
                    if changed.rowcount != 1: raise ProviderConflict('cross-model terminal changed')
                finally:
                    try: set_writer(connection)
                    except Exception:
                        connection.invalidate()
                        raise
                return {'version':1,'state':'auth_no_output_closed','bindingID':binding.id,
                        'bindingRevision':binding.revision+1,'attemptID':attempt_id,'attemptGeneration':attempt_generation,
                        'coordinatorRevision':value['revision']}
            if outcome is None and value['state'] == 'committed' and binding.state == 'committed':
                result = self._selection_from_binding(db, binding, sticky=True).to_wire()
                result['crossModelAttempt'] = {'version':1,'attemptID':attempt_id,'attemptGeneration':1,'coordinatorRevision':value['revision']}
                return result
            if outcome is None and (value['state'] != 'admitted' or binding.state != 'uncommitted'):
                raise ProviderConflict('cross-model commitment unavailable')
            if outcome is not None and ('last_control' in active or value['state'] not in ('admitted','committed')):
                raise ProviderConflict('cross-model attempt already stopped')
            timestamp = _utc(self._clock())
            if binding.attempts is not None and type(binding.attempts) is not list:
                raise ProviderValidationError('cross-model attempt inventory unavailable')
            entries = list(binding.attempts or ())
            if len(entries) >= 256: raise ProviderValidationError('cross-model attempt inventory unavailable')
            if outcome is not None:
                entries.append({'attempt':int(binding.attempt),'outcome':outcome,'at':timestamp.isoformat(),
                    'committed':binding.state == 'committed','account_id':binding.selected_account_id})
                active['last_control'] = {'outcome':outcome,'accounting':'unavailable'}
            state = 'committed' if outcome is None or value['state'] == 'committed' else 'unknown'
            value['state'] = state; value['revision'] += 1; active['coordinator_revision'] = value['revision']
            new = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
            attempts_json = json.dumps(entries, separators=(',', ':'), allow_nan=False)
            if len(new.encode('utf-8')) > 65536 or len(attempts_json.encode('utf-8')) > 65536:
                raise ProviderValidationError('cross-model control unavailable')
            from src.openclank.provider_cross_model import set_writer
            def authorize(op, bid, owner, root, actor, prior, replacement):
                try:
                    admission.assert_current()
                    return int(not callback_authority._generation_closed.is_set() and admission._callback is callback_authority
                        and callback_authority._store is self and (op,bid,owner,root,actor,prior,replacement) ==
                        ('update',binding.id,binding.owner,binding.root_operation_id,binding.actor_scope_key,old,new))
                except Exception: return 0
            set_writer(connection, authorize)
            try:
                changed=connection.exec_driver_sql('UPDATE provider_operation_bindings SET state=?, committed_at=?, attempts=?, revision=?, cross_model_coordinator=? WHERE id=? AND revision=? AND cross_model_coordinator=?',
                    ('committed' if outcome is None else binding.state, timestamp if outcome is None else binding.committed_at,
                     attempts_json, binding.revision+1, new, binding.id, expected_binding_revision, old))
                if changed.rowcount != 1: raise ProviderConflict('cross-model control changed')
            finally:
                try: set_writer(connection)
                except Exception:
                    connection.invalidate()
                    raise
            db.expire_all()
            current = self._cross_model_binding_identity(db, admission, binding_id)
            result = self._selection_from_binding(db, current, sticky=True).to_wire()
            result['crossModelAttempt'] = {'version':1,'attemptID':attempt_id,'attemptGeneration':1,'coordinatorRevision':value['revision'],
                **({'accounting':'unavailable'} if outcome is not None else {})}
            return result

    def lease_credential(
        self,
        *,
        owner: str,
        holder_id: str,
        root_operation_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        account_id: Optional[str] = None,
        model_id: Optional[str] = None,
        expected_credential_revision: int,
        binding_id: Optional[str] = None,
        expected_binding_revision: Optional[int] = None,
        grant_id: Optional[str] = None,
        now: Optional[datetime] = None,
        ttl_seconds: float = 30,
    ) -> CredentialLeaseGrant:
        """Disclose a credential only for its persisted exact operation binding."""

        owner = _owner(owner)
        holder_id = _required(holder_id, "holder_id")
        root_operation_id = _required(root_operation_id, "root_operation_id") if root_operation_id is not None else None
        connection_id = _required(connection_id, "connection_id") if connection_id is not None else None
        account_id = _required(account_id, "account_id") if account_id is not None else None
        model_id = _required(model_id, "model_id") if model_id is not None else None
        grant_id = _required(grant_id, "grant_id") if grant_id else None
        ttl_seconds = float(ttl_seconds)
        if ttl_seconds <= 0 or ttl_seconds > 60:
            raise ProviderValidationError(
                "credential lease TTL must be positive and at most 60 seconds"
            )
        with self._credential_transaction() as db:
            binding_query = db.query(ProviderOperationBinding).filter(
                ProviderOperationBinding.owner == owner,
            )
            if connection_id is not None:
                binding_query = binding_query.filter(ProviderOperationBinding.connection_id == connection_id)
            if root_operation_id is not None:
                binding_query = binding_query.filter(
                    ProviderOperationBinding.root_operation_id == root_operation_id
                )
            if binding_id is not None:
                binding_query = binding_query.filter(
                    ProviderOperationBinding.id == _required(binding_id, "binding_id")
                )
            bindings = binding_query.limit(2).all()
            if len(bindings) > 1:
                raise ProviderValidationError("credential lease requires an exact provider binding")
            binding = bindings[0] if bindings else None
            if binding is None:
                raise ProviderNotFound("provider operation binding was not found")
            self._reject_enrolled(db, binding)
            root_operation_id = binding.root_operation_id
            if binding.actor_id is not None and binding_id is None:
                raise ProviderValidationError("actor credential lease requires binding_id")
            if expected_binding_revision is not None:
                self._require_revision(binding.revision, expected_binding_revision)
            if connection_id is not None and binding.connection_id != connection_id:
                raise ShareDenied("credential request does not match its operation binding")
            if account_id is not None and binding.selected_account_id != account_id:
                raise ShareDenied("credential request does not match its operation binding")
            if model_id is not None and binding.model_id != model_id:
                raise ShareDenied("credential request does not match its operation binding")
            if grant_id is not None and binding.grant_id != grant_id:
                raise ShareDenied("credential request does not match its operation binding")
            connection_id = binding.connection_id
            account_id = binding.selected_account_id
            model_id = binding.model_id
            grant_id = binding.grant_id
            if not account_id:
                raise ProviderNotFound("provider operation binding has no credential account")
            account = self._current_credential_authority(db, binding, expected_credential_revision)
            timestamp = _utc(now if now is not None else self._clock())
            lease, grant = self._prepare_credential_lease(binding=binding, account=account,
                owner=owner, holder_id=holder_id, timestamp=timestamp, ttl_seconds=ttl_seconds)
            expired = db.query(ProviderCredentialLease).filter(ProviderCredentialLease.expires_at <= timestamp)
            self._unenrolled_lease_query(db,expired).delete(synchronize_session=False)
            db.add(lease)
            db.flush()
            return grant

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

        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK:
            return self._consume_credential_lease_locked(
                owner=owner, lease_id=lease_id, holder_id=holder_id, now=now,
            )

    def _consume_credential_lease_locked(
        self, *, owner: str, lease_id: str, holder_id: str,
        now: Optional[datetime] = None,
    ) -> CredentialLeaseGrant:
        owner = _owner(owner)
        lease_id = _required(lease_id, "lease_id")
        holder_id = _required(holder_id, "holder_id")
        digest = self._credential_lease_digest(lease_id)
        with self._credential_transaction() as db:
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
            binding = db.query(ProviderOperationBinding).filter(
                ProviderOperationBinding.id == lease.binding_id,
                ProviderOperationBinding.owner == owner,
                ProviderOperationBinding.root_operation_id == lease.root_operation_id,
            ).first()
            if binding is None:
                raise ProviderNotFound("provider credential lease binding is unavailable")
            self._reject_enrolled(db, binding)
            if (binding.policy_snapshot or {}).get("private_authority") is not None:
                raise ProviderValidationError("private driver credential consumption is unavailable")

            expires_at = _utc(lease.expires_at)
            if (binding.credential_owner != lease.credential_owner or binding.connection_id != lease.connection_id
                or binding.selected_account_id != lease.account_id or binding.model_id != lease.model_id
                or binding.grant_id != lease.grant_id):
                raise ProviderNotFound("provider credential lease binding changed")
            account = self._current_credential_authority(db, binding, lease.credential_revision)
            timestamp = _utc(now if now is not None else self._clock())

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
                    binding_id=binding.id,
                    session_id=binding.engine_session_id,
                    actor_id=binding.actor_id,
                    execution_revision=binding.execution_revision,
                    root_operation_id=binding.root_operation_id,
                    model_route_id=binding.model_route_id,
                    scope_kind=("actor_execution" if binding.actor_id is not None else "auxiliary_operation"),
                    operation_id=binding.operation_id,
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
                binding_id=binding.id,
                session_id=binding.engine_session_id,
                actor_id=binding.actor_id,
                execution_revision=binding.execution_revision,
                root_operation_id=binding.root_operation_id,
                model_route_id=binding.model_route_id,
                scope_kind=("actor_execution" if binding.actor_id is not None else "auxiliary_operation"),
                operation_id=binding.operation_id,
            )

    @staticmethod
    def _lease_digest(token: str) -> str:
        return keyed_digest(
            token,
            context="provider-refresh-lease",
        )

    def _refresh_binding_authority(self, db: Any, *, owner: str, binding_id: str,
                                   proof: Any, account_id: str) -> ProviderOperationBinding:
        binding = db.query(ProviderOperationBinding).filter_by(id=binding_id, owner=owner).first()
        if binding is None or binding.selected_account_id != account_id:
            raise ShareDenied("refresh binding is unavailable")
        connection = self._require_connection(db, binding.credential_owner, binding.connection_id)
        account = self._require_account(db, binding.credential_owner, account_id)
        if (not connection.enabled or connection.family_id != "anthropic" or connection.billing_lane != "subscription"
            or binding.billing_lane != connection.billing_lane or account.connection_id != connection.id
            or _binding_method_policy(binding) != proof):
            raise ShareDenied("refresh binding authority changed")
        _check_method_policy(proof, binding.credential_owner, account)
        if binding.grant_id:
            grant = db.query(ProviderShareGrant).filter_by(id=binding.grant_id, owner=binding.credential_owner,
                recipient=owner, connection_id=connection.id, state="active").first()
            if (grant is None or grant.billing_lane != connection.billing_lane
                or not AccountSelector.parse(grant.account_selector).includes(account.id)
                or not ModelSelector.parse(grant.model_selector).includes(binding.model_route_id)):
                raise ShareDenied("refresh grant is no longer active")
        elif binding.credential_owner != owner:
            raise ShareDenied("refresh share authority is unavailable")
        return binding

    @staticmethod
    def _refresh_claims(token: str) -> dict[str, Any]:
        try:
            prefix, secret, encoded = token.split(".")
            if prefix != "ocrefresh1" or len(secret) < 32 or len(encoded) > 4096:
                raise ValueError()
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            claims = json.loads(raw)
            if not isinstance(claims, dict) or set(claims) != {"bindingID", "methodPolicy", "recipient", "credentialOwner"}:
                raise ValueError()
            if any(type(claims[k]) is not str or not claims[k] or len(claims[k]) > 256 for k in ("bindingID", "recipient", "credentialOwner")):
                raise ValueError()
            return claims
        except (ValueError, TypeError, UnicodeError) as exc:
            raise RefreshLeaseInvalid("refresh lease policy claims are invalid") from exc

    @_preferences_before_provider
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
        binding_id: Optional[str] = None,
        method_policy: Any = None,
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
            binding = self._refresh_binding_authority(db, owner=owner, binding_id=binding_id, proof=method_policy, account_id=account_id) if binding_id else None
            credential_owner = binding.credential_owner if binding else owner
            account = self._require_account(db, credential_owner, account_id)
            connection = self._require_connection(db, credential_owner, account.connection_id)
            if connection.family_id == "anthropic" and connection.billing_lane == "subscription" and binding is None:
                raise ShareDenied("Claude refresh requires an exact binding")
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
            if binding is not None:
                claims = {"bindingID": binding.id, "methodPolicy": dict(method_policy),
                          "recipient": owner, "credentialOwner": credential_owner}
                encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()
                token = "ocrefresh1." + token + "." + encoded
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
                method_policy=method_policy if binding_id else None,
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

    @_preferences_before_provider
    def renew_refresh_lease(
        self,
        *,
        owner: str,
        account_id: Optional[str] = None,
        token: str,
        now: Optional[datetime] = None,
        ttl_seconds: float = 60,
        binding_id: Optional[str] = None,
        method_policy: Any = None,
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
            if token.startswith("ocrefresh1."):
                claims = self._refresh_claims(token)
                if claims["bindingID"] != binding_id or claims["methodPolicy"] != method_policy or claims["recipient"] != owner:
                    raise RefreshLeaseInvalid("refresh lease policy changed")
                binding = self._refresh_binding_authority(db, owner=owner, binding_id=binding_id, proof=method_policy, account_id=row.account_id)
                if claims["credentialOwner"] != binding.credential_owner:
                    raise RefreshLeaseInvalid("refresh lease owner changed")
                account = self._require_account(db, binding.credential_owner, row.account_id)
            else:
                account = self._require_account(db, owner, row.account_id)
                connection = self._require_connection(db, owner, account.connection_id)
                if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                    raise RefreshLeaseInvalid("Claude refresh lease lacks policy proof")
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
                method_policy=method_policy if binding_id else None,
            )

    @_preferences_before_provider
    def commit_refresh(
        self,
        *,
        owner: str,
        account_id: str,
        token: str,
        expected_credential_revision: int,
        credentials: Mapping[str, Any],
        now: Optional[datetime] = None,
        binding_id: Optional[str] = None,
        method_policy: Any = None,
        expected_connection_id: Optional[str] = None,
    ) -> ProviderAccount:
        owner = _owner(owner)
        account_id = _required(account_id, "account_id")
        timestamp = _utc(now or self._clock())
        with self._transaction() as db:
            lease = self._require_lease(
                db,
                token=token,
                now=timestamp,
                account_id=account_id,
            )
            if token.startswith("ocrefresh1."):
                claims = self._refresh_claims(token)
                if claims["bindingID"] != binding_id or claims["methodPolicy"] != method_policy or claims["recipient"] != owner:
                    raise RefreshLeaseInvalid("refresh lease policy changed")
                binding = self._refresh_binding_authority(db, owner=owner, binding_id=binding_id, proof=method_policy, account_id=account_id)
                if claims["credentialOwner"] != binding.credential_owner:
                    raise RefreshLeaseInvalid("refresh lease owner changed")
                account = self._require_account(db, binding.credential_owner, account_id)
            else:
                account = self._require_account(db, owner, account_id)
                connection = self._require_connection(db, owner, account.connection_id)
                if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                    raise RefreshLeaseInvalid("Claude refresh lease lacks policy proof")
            if expected_connection_id is not None and account.connection_id != expected_connection_id:
                raise ProviderValidationError("refresh account belongs to a different connection")
            previous_credential_revision = int(account.credential_version)
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
                or_(ProviderOperationBinding.state == "uncommitted", ProviderOperationBinding.id == binding_id) if binding_id else ProviderOperationBinding.state == "uncommitted",
            ).update(
                {
                    ProviderOperationBinding.credential_revision:
                    next_credential_revision,
                },
                synchronize_session=False,
            )
            # Old short-lived disclosures are no longer valid after the CAS.
            self._unenrolled_lease_query(db,db.query(ProviderCredentialLease).filter(
                ProviderCredentialLease.account_id == account.id,
                ProviderCredentialLease.credential_revision
                == previous_credential_revision,
            )).delete(synchronize_session=False)
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
            if token.startswith("ocrefresh1."):
                claims = self._refresh_claims(token)
                if claims["recipient"] != owner:
                    raise RefreshLeaseInvalid("refresh lease recipient changed")
                self._require_account(db, claims["credentialOwner"], row.account_id)
            else:
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
                self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,grant_id=target.id)
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
                self._invalidate_credential_leases(db,owner=owner,connection_id=connection.id,grant_id=grant_id)
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
            if row.account_selector != account_selector.as_json() or row.model_selector != model_selector.as_json():
                self._invalidate_credential_leases(db,owner=owner,connection_id=row.connection_id,grant_id=row.id)
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
            if row.state != 'revoked':
                self._invalidate_credential_leases(db,owner=owner,connection_id=row.connection_id,grant_id=row.id)
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
            reauth_counts = dict(db.query(ProviderAccount.connection_id, func.count(ProviderAccount.id))
                .join(ProviderAccountHealth, ProviderAccountHealth.account_id == ProviderAccount.id)
                .filter(ProviderAccount.owner == owner, ProviderAccount.deleted_at.is_(None),
                        ProviderAccount.enabled.is_(True), ProviderAccountHealth.state == "reauth_required")
                .group_by(ProviderAccount.connection_id).all())
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
                reauth_counts=reauth_counts,
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
        private_authority: Optional[Mapping[str, Any]] = None,
        trusted_options: Optional[Mapping[str, Any]] = None,
        trusted_logging_policy: Optional[Mapping[str, Any]] = None,
        _fresh=None,
    ) -> dict[str, Any]:
        selection = self.bind_account(
            owner=owner,
            root_operation_id=_required(
                params.get("rootOperationID"),
                "rootOperationID",
            ),
            scope_kind=_required(params.get("scopeKind"), "scopeKind"),
            operation_id=self._optional_param(params, "operationID"),
            session_id=self._optional_param(params, "sessionID"),
            actor_id=self._optional_param(params, "actorID"),
            execution_revision=(
                int(params["executionRevision"])
                if params.get("executionRevision") is not None
                else None
            ),
            model_route_id=self._optional_param(params, "modelRouteID"),
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
            method_policy=params.get("methodPolicy"),
            private_authority=private_authority,
            trusted_options=trusted_options,
            trusted_logging_policy=trusted_logging_policy,
            _fresh=_fresh,
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
        trusted_refresh_check=None,
        trusted_success_check=None,
    ) -> dict[str, Any]:
        selection = self.record_account_attempt(
            owner=owner,
            binding_id=_required(params.get("bindingID"), "bindingID"),
            expected_revision=int(params.get("expectedRevision")),
            account_id=self._optional_param(params, "accountID"),
            outcome=_required(params.get("outcome"), "outcome"),
            evidence=params.get("evidence"),
            dispatch_observation=params.get('dispatchObservation'),
            refresh_continuity=params.get('refreshContinuity'),
            trusted_refresh_check=trusted_refresh_check,
            success_continuity=params.get('successContinuity'),
            trusted_success_check=trusted_success_check,
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
            binding_id=_required(params.get("bindingID"), "bindingID"),
            expected_binding_revision=int(params.get("expectedBindingRevision")),
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
            binding_id=params.get("bindingID"), method_policy=params.get("methodPolicy"),
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
            binding_id=params.get("bindingID"), method_policy=params.get("methodPolicy"),
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
        account = self.commit_refresh(
            owner=owner,
            account_id=account_id,
            token=_required(params.get("leaseID"), "leaseID"),
            expected_credential_revision=int(params.get("expectedRevision")),
            credentials=_json_mapping(params.get("credential")),
            binding_id=params.get("bindingID"), method_policy=params.get("methodPolicy"),
            expected_connection_id=connection_id,
        )
        return self._wire_account(owner=account.owner, account=account)

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
        private_authority: Optional[Mapping[str, Any]] = None,
        trusted_options: Optional[Mapping[str, Any]] = None,
        trusted_logging_policy: Optional[Mapping[str, Any]] = None,
        trusted_refresh_check=None,
        trusted_success_check=None,
    ) -> dict[str, Any]:
        """Dispatch one secret-bearing ACP extMethod using the v1 wire shapes."""

        if not isinstance(params, Mapping):
            raise ProviderValidationError("managed provider params must be an object")
        handlers = {
            "_openclank/provider-store/v1/account/bind": lambda: self.managed_account_bind(
                owner=owner,
                params=params,
                private_authority=private_authority,
                trusted_options=trusted_options,
                trusted_logging_policy=trusted_logging_policy,
            ),
            "_openclank/provider-store/v1/account/commit": lambda: self.managed_account_commit(
                owner=owner,
                params=params,
            ),
            "_openclank/provider-store/v1/account/attempt": lambda: self.managed_account_attempt(
                owner=owner,
                params=params,
                trusted_refresh_check=trusted_refresh_check,
                trusted_success_check=trusted_success_check,
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
        # Until the native method-policy proof is negotiated, stored Claude
        # subscription credentials cannot use the legacy managed transport.
        # Keep the preference lock through the provider transaction so a policy
        # update cannot race an eventual authority decision. No IPC occurs here.
        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK:
            db = self._session_factory()
            try:
                connection = None
                binding_id = params.get("bindingID")
                if binding_id:
                    binding = db.query(ProviderOperationBinding).filter_by(
                        owner=owner, id=binding_id
                    ).first()
                    if binding is not None:
                        connection = db.query(ProviderConnection).filter_by(
                            owner=binding.credential_owner, id=binding.connection_id
                        ).first()
                elif params.get("connectionID"):
                    credential_owner = owner
                    if params.get("grantID"):
                        grant = db.query(ProviderShareGrant).filter_by(
                            id=params["grantID"], recipient=owner, state="active"
                        ).first()
                        if grant is not None:
                            credential_owner = grant.owner
                    connection = db.query(ProviderConnection).filter_by(
                        owner=credential_owner, id=params["connectionID"]
                    ).first()
                elif method.endswith("/refresh/renew") and params.get("leaseID"):
                    lease = db.query(ProviderRefreshLease).filter_by(
                        token_digest=self._lease_digest(params["leaseID"])
                    ).first()
                    if lease is not None:
                        account = db.query(ProviderAccount).filter_by(id=lease.account_id, owner=owner).first()
                        if account is not None:
                            connection = db.query(ProviderConnection).filter_by(
                                owner=owner, id=account.connection_id
                            ).first()
                if (connection is not None and connection.family_id == "anthropic"
                        and connection.billing_lane == "subscription"):
                    if method.endswith(("/account/bind", "/account/commit", "/account/attempt", "/credential/lease")):
                        if binding_id:
                            proof = _binding_method_policy(binding)
                            account = db.query(ProviderAccount).filter_by(id=binding.selected_account_id, owner=binding.credential_owner).first()
                            if account is None or account.connection_id != connection.id:
                                raise ProviderValidationError("Claude subscription account is unavailable")
                            _check_method_policy(proof, binding.credential_owner, account)
                        else:
                            _check_method_policy(params.get("methodPolicy"), credential_owner)
                    elif method.endswith(("/refresh/acquire", "/refresh/renew", "/refresh/commit")) and binding_id:
                        if binding.actor_id is None:
                            raise ProviderValidationError("Claude auxiliary refresh is unavailable")
                        _check_method_policy(_binding_method_policy(binding), binding.credential_owner)
                        if params.get("methodPolicy") != _binding_method_policy(binding):
                            raise ProviderValidationError("Claude refresh policy proof changed")
                    else:
                        raise ProviderValidationError("Claude subscription managed refresh is unavailable")
            finally:
                db.close()
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

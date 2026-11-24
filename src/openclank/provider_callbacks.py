"""Capability-bound ACP callbacks for the managed Open Clank engine.

The child receives no owner selector and no vault snapshot.  Each callback is
closed over one supervisor-owned principal and worker generation, validates
both sides of the pinned JSON contract, and discloses credentials only through
the exact operation lease implemented by :class:`ProviderStore`.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import json
from dataclasses import dataclass
from datetime import timezone
import hashlib
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Mapping, Optional

from core.database import SessionLocal
from core.provider_models import (
    ProviderConnection,
    ProviderModelRoute,
    ProviderShareGrant,
)
from src.openclank.managed_protocol import (
    ManagedMethodValidationError,
    HOST_CALLBACK_METHODS,
    METHOD_DIRECTIONS,
    validate_managed_method_request,
    validate_managed_method_result,
    prepare_managed_method_validation,
)
from src.openclank.generated.managed_provider_contract import (
    OPERATION_METHODS,
    PROVIDER_STORE_METHODS,
    SESSION_METHODS,
    LOGGING_METHODS,
)
from src.openclank.provider_store import AccountSelector, ModelSelector
from src.openclank.artifacts import ArtifactError, ArtifactStore
from src.openclank.local_executor import LocalExecutorBroker, LocalExecutorError
from src.openclank.operation_journal import (
    OperationJournalError,
    OperationJournalStore,
    canonical_request_hash,
)
from src.openclank.provider_store import ProviderStore, ProviderStoreError


_GENERATED_CALLBACK_METHODS = tuple(
    method
    for method in (*PROVIDER_STORE_METHODS, *OPERATION_METHODS, *LOGGING_METHODS)
    if METHOD_DIRECTIONS[method] == "engine_to_host"
)
if frozenset(_GENERATED_CALLBACK_METHODS) != HOST_CALLBACK_METHODS - frozenset(SESSION_METHODS):
    raise RuntimeError("generated managed callback direction set is inconsistent")

PROVIDER_CALLBACK_METHODS = tuple(
    method for method in _GENERATED_CALLBACK_METHODS if method.startswith("_openclank/provider-store/")
)
OPERATION_CALLBACK_METHODS = tuple(
    method for method in _GENERATED_CALLBACK_METHODS if method.startswith("_openclank/operations/")
)
MANAGED_CALLBACK_METHODS = _GENERATED_CALLBACK_METHODS

_SENSITIVE_METADATA_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


class ManagedProviderCallbackError(RuntimeError):
    """A deliberately secret-free error safe to return over JSON-RPC."""


class ManagedProviderRouteError(ManagedProviderCallbackError):
    pass


_FRESH_MAIN_SEAL = object()

@dataclass(frozen=True)
class _FreshMainBinding:
    admission: object
    callback: object
    root: object
    request_json: str
    context_json: str
    selection: object
    seal: object

    def assert_current(self):
        if self.seal is not _FRESH_MAIN_SEAL or self.admission._root is not self.root or self.admission._callback is not self.callback:
            raise ManagedProviderCallbackError('fresh main authority unavailable')
        from src.openclank.provider_human_policy import CapturedProviderPolicy
        if (type(self.selection) is not CapturedProviderPolicy
                or self.root.foreground_attachment.get('provider_policy_selection') is not self.selection):
            raise ManagedProviderCallbackError('fresh main policy unavailable')
        self.selection.assert_current()
        self.admission.assert_current()


@dataclass
class _SearchHandoff:
    dispatch_key: str
    fingerprint: str
    input_fingerprint: str
    session_id: str
    root_operation_id: str
    deadline: float
    prepared: Any
    wire: dict[str, Any]
    receipt_raw: Optional[str] = None
    receipt: Optional[dict[str, Any]] = None
    artifact: Optional[dict[str, Any]] = None
    dispatch_future: Any = None
    failure: Optional[BaseException] = None
    timer: Any = None
    terminal_lock: Any = None
    dispatch_deadline: float = 0.0
    dispatch_task: Any = None


@dataclass(frozen=True)
class ProviderMethodPolicyProof:
    credential_owner: str
    policy_revision: int

    def to_wire(self) -> dict[str, Any]:
        return {"version": 1, "methodID": "anthropic.claude_subscription",
                "credentialOwner": self.credential_owner, "policyRevision": self.policy_revision}


@dataclass(frozen=True)
class ManagedProviderRouteContext:
    connection_id: str
    provider_id: str
    billing_lane: str
    model_id: str
    model_route_id: str
    grant_id: Optional[str] = None
    grant_revision: Optional[int] = None
    method_policy: Optional[ProviderMethodPolicyProof] = None
    catalog_revision: int = 1
    option_route_id: str = ""
    option_capabilities: Optional[Mapping[str, Any]] = None
    # Private route identity, not an authentication method or permission.
    adapter_id: str = ""
    connection_kind: str = ""

    def to_wire(self, *, root_operation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rootOperationID": root_operation_id,
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "modelID": self.model_id,
            "modelRouteID": self.model_route_id,
        }
        if self.method_policy is not None:
            result["methodPolicy"] = self.method_policy.to_wire()
        if self.grant_id:
            result["grantID"] = self.grant_id
            if self.grant_revision is not None:
                result["grantRevision"] = int(self.grant_revision)
        return result


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text or "\x00" in text:
        raise ManagedProviderRouteError(f"{label} is required")
    return text


class ManagedProviderCallbacks:
    """One owner/generation's managed provider callback authority."""

    def __init__(
        self,
        *,
        owner: str,
        holder_id: str,
        store: Optional[ProviderStore] = None,
        session_factory=SessionLocal,
        operation_journal: Optional[OperationJournalStore] = None,
        artifact_store: Optional[ArtifactStore] = None,
        executor_broker: Optional[LocalExecutorBroker] = None,
        capture_store=None,
        search_context_resolver=None,
        search_delegate=None,
        search_clock=None,
        max_search_handoffs: int = 64,
    ) -> None:
        self._owner = _required_text(owner, "managed worker owner").lower()
        self._holder_id = _required_text(holder_id, "managed worker generation")
        self._store = store or ProviderStore(session_factory)
        self._session_factory = session_factory
        self._operation_journal = operation_journal or OperationJournalStore(
            session_factory
        )
        if artifact_store is None:
            from src.constants import DATA_DIR

            artifact_store = ArtifactStore(
                Path(DATA_DIR) / "model-artifacts",
                session_factory=session_factory,
            )
        self._artifact_store = artifact_store
        self._executor_broker = executor_broker
        if capture_store is None:
            from src.openclank.logging_capture_store import CAPTURE
            capture_store = CAPTURE
        self._capture = capture_store
        self._search_context_resolver = search_context_resolver
        self._actor_execution_context_resolver = None
        self._search_delegate = search_delegate
        self._search_clock = search_clock or time.monotonic
        self._search_handoffs: dict[str, _SearchHandoff] = {}
        self._search_dispatches: dict[str, str] = {}
        self._search_dispatch_turns: dict[str, tuple[str, str]] = {}
        self._search_expired: set[str] = set()
        self._search_cancelled: set[str] = set()
        self._search_cancelled_turns: dict[str, tuple[str, str]] = {}
        self._search_closed = False
        self._search_handoff_lock = threading.RLock()
        self._max_search_handoffs = max(1, min(int(max_search_handoffs), 256))
        self._logging_operations: dict[str, dict[str, Any]] = {}
        self._logging_contexts: dict[str, dict[str, Any]] = {}
        self._logging_context_lock = __import__("threading").RLock()
        self._generation_lock = __import__("threading").RLock()
        self._generation_closed = threading.Event()
        # Host-only, generation-local admissions; never durable prompt content.
        self._prepared_operations: dict[tuple[str, str], dict[str, Any]] = {}
        self._retired_prepared_operations: set[str] = set()

    def _dispatch_provider_generation(self, method: str, request: Mapping[str, Any], private_authority=None, auxiliary=None, trusted_options=None) -> dict[str, Any]:
        with self._generation_lock:
            if self._generation_closed.is_set():
                raise ManagedProviderCallbackError("managed provider generation is closed")
            if auxiliary is not None:
                identity, route = auxiliary
                self._validate_auxiliary_operation(identity, route)
                self._validate_prepared_auxiliary(identity, route)
            if method == '_openclank/provider-store/v1/account/bind' and request.get('scopeKind') == 'actor_execution':
                # The store acquires the same reentrant lock before its provider
                # transaction. Never use an engine-supplied capture policy.
                from routes.prefs_routes import PREFERENCE_LOCK
                with PREFERENCE_LOCK:
                    snapshot = None
                    try:
                        policy = self._capture.policy.get_policy(self._owner)
                        snapshot = {'version':1, 'revision':policy['revision'],
                            **{key:policy[key] for key in ('advanced_enabled','request_body_enabled','response_body_enabled','binary_body_enabled')},
                            'private':private_authority is not None}
                        from src.openclank.provider_store import _validate_logging_policy_snapshot
                        snapshot = _validate_logging_policy_snapshot(snapshot)
                    except Exception:
                        # Logging failure must not become a provider failure.
                        snapshot = None
                    return self._store.handle_managed_method(owner=self._owner,holder_id=self._holder_id,method=method,params=request,
                        **({'private_authority':private_authority} if private_authority else {}),
                        **({'trusted_options':trusted_options} if trusted_options is not None else {}),
                        **({'trusted_logging_policy':snapshot} if snapshot is not None else {}))
            def refresh_check(db, binding, continuity, observation):
                return self._capture.refresh_continuity_current(db,owner=self._owner,holder_id=self._holder_id,
                    admission_id=continuity['admissionID'],binding=binding,previous_revision=continuity['previousCredentialRevision'],
                    dispatch_id=observation['captureDispatchID'])
            def success_check(db, binding, continuity, observation):
                return self._capture.refresh_continuity_current(db,owner=self._owner,holder_id=self._holder_id,
                    admission_id=continuity['admissionID'],binding=binding,previous_revision=continuity.get('previousCredentialRevision',continuity['credentialRevision']),
                    dispatch_id=observation['captureDispatchID'],binding_revision_delta=2)
            return self._store.handle_managed_method(owner=self._owner, holder_id=self._holder_id, method=method, params=request,
                **({'trusted_refresh_check':refresh_check} if method=='_openclank/provider-store/v1/account/attempt' and request.get('refreshContinuity') is not None else {}),
                **({'trusted_success_check':success_check} if method=='_openclank/provider-store/v1/account/attempt' and request.get('successContinuity') is not None else {}),
                **({"private_authority": private_authority} if private_authority else {}),
                **({"trusted_options": trusted_options} if trusted_options is not None else {}))

    def close_provider_generation(self) -> None:
        self._generation_closed.set()
        with self._generation_lock:
            with self._logging_context_lock:
                for entry in self._prepared_operations.values():
                    operation_id = entry.get("operation_id")
                    context = self._logging_operations.get(operation_id)
                    if context is not None and context.get("prepared_registration") == entry["registration"]:
                        context.pop("prepared_registration", None)
                        context.pop("prepared_options", None)
                        if context.pop("prepared_only", False):
                            self._logging_operations.pop(operation_id, None)
                self._prepared_operations.clear()
            self._retired_prepared_operations.clear()
            self._store.revoke_worker_generation(owner=self._owner, holder_id=self._holder_id)

    def register_prepared_operation(self, root_operation_id: str, idempotency_key: str,
                                    request_hash: str, options: Mapping[str, Any], route: Mapping[str, Any]) -> str:
        """Register host-prepared nonsecret option authority for one operation."""
        import secrets
        if (not isinstance(root_operation_id, str) or not root_operation_id
            or not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128
            or not isinstance(request_hash, str) or len(request_hash) != 64
            or any(char not in "0123456789abcdef" for char in request_hash)):
            raise ManagedProviderCallbackError("invalid prepared operation identity")
        if not isinstance(options, Mapping) or set(options) != {"version", "routeID", "optionRouteID", "catalogRevision", "model", "options"}:
            raise ManagedProviderCallbackError("invalid prepared model options")
        model, selected = options.get("model"), options.get("options")
        if (options.get("version") != 1 or type(options.get("version")) is not int
            or type(options.get("catalogRevision")) is not int or options["catalogRevision"] < 1
            or any(not isinstance(options.get(key), str) or not options[key] for key in ("routeID", "optionRouteID"))
            or not isinstance(model, Mapping) or set(model) != {"providerID", "modelID"}
            or any(not isinstance(model.get(key), str) or not model[key] for key in ("providerID", "modelID"))
            or not isinstance(selected, Mapping) or set(selected) - {"reasoning_effort"}
            or any(not isinstance(value, str) or not value for value in selected.values())):
            raise ManagedProviderCallbackError("invalid prepared model options")
        required_route = {"connectionID", "providerID", "billingLane", "modelRouteID", "modelID"}
        if (not isinstance(route, Mapping) or set(route) - (required_route | {"grantID"})
            or not required_route <= set(route)
            or any(not isinstance(value, str) or not value for value in route.values())
            or route["modelRouteID"] != options["routeID"]
            or model != {"providerID": route["connectionID"], "modelID": route["modelID"]}):
            raise ManagedProviderCallbackError("invalid prepared operation route")
        frozen_route = dict(route)
        frozen = {"version": 1, "routeID": options["routeID"], "optionRouteID": options["optionRouteID"],
                  "catalogRevision": options["catalogRevision"], "model": dict(model), "options": dict(selected)}
        key = (root_operation_id, idempotency_key)
        with self._generation_lock:
            if self._generation_closed.is_set():
                raise ManagedProviderCallbackError("managed provider generation is closed")
            with self._logging_context_lock:
                if self._retired_prepared_operations:
                    db = None
                    try:
                        from core.operation_models import OperationJournal
                        db = self._session_factory()
                        active = {row.id for row in db.query(OperationJournal).filter(
                            OperationJournal.owner == self._owner,
                            OperationJournal.id.in_(self._retired_prepared_operations),
                            OperationJournal.state.notin_(("complete", "failed", "cancelled")),
                        ).all()}
                        self._retired_prepared_operations.intersection_update(active)
                    except Exception:
                        pass  # Unknown journal state must remain denied.
                    finally:
                        if db is not None:
                            db.close()
                if key in self._prepared_operations or len(self._prepared_operations) + len(self._retired_prepared_operations) >= 1000:
                    raise ManagedProviderCallbackError("prepared operation is already active or capacity unavailable")
                registration = secrets.token_urlsafe(32)
                self._prepared_operations[key] = {"registration": registration, "request_hash": request_hash,
                                                  "options": frozen, "route": frozen_route, "operation_id": None}
                return registration

    def retire_prepared_operation(self, root_operation_id: str, idempotency_key: str,
                                  registration: str) -> bool:
        """Retire only the exact registration owned by this caller."""
        with self._generation_lock:
            with self._logging_context_lock:
                key = (root_operation_id, idempotency_key)
                entry = self._prepared_operations.get(key)
                if entry is None or entry["registration"] != registration:
                    return False
                context = self._logging_operations.get(entry.get("operation_id"))
                if context is not None and context.get("prepared_registration") == registration:
                    context.pop("prepared_registration", None)
                    context.pop("prepared_options", None)
                    if context.pop("prepared_only", False):
                        self._logging_operations.pop(entry.get("operation_id"), None)
                operation_id = entry.get("operation_id")
                if operation_id:
                    # Only an exact owned terminal (or deleted) journal proves late calls
                    # are already denied. Unknown state retains a generation marker.
                    terminal = False
                    db = None
                    try:
                        from core.operation_models import OperationJournal
                        db = self._session_factory()
                        row = db.query(OperationJournal).filter_by(id=operation_id, owner=self._owner,
                            root_operation_id=root_operation_id, idempotency_key=idempotency_key).first()
                        terminal = row is None or row.state in {"complete", "failed", "cancelled"}
                    except Exception:
                        terminal = False
                    finally:
                        if db is not None:
                            db.close()
                    if not terminal:
                        self._retired_prepared_operations.add(operation_id)
                del self._prepared_operations[key]
                return True

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def holder_id(self) -> str:
        return self._holder_id

    def set_search_context_resolver(self, resolver) -> None:
        """Install the bridge's host-derived session authority resolver."""
        if not callable(resolver):
            raise ValueError("managed search context resolver is required")
        self._search_context_resolver = resolver

    def set_actor_execution_context_resolver(self, resolver) -> None:
        """Install the bridge's verified native actor-lineage resolver."""
        if not callable(resolver):
            raise ValueError("managed actor execution context resolver is required")
        self._actor_execution_context_resolver = resolver

    async def _validate_actor_execution(
        self, identity: Mapping[str, Any], *, model_id: Optional[str] = None,
        model_route_id: Optional[str] = None, provider_id: Optional[str] = None,
        connection_id: Optional[str] = None, billing_lane: Optional[str] = None,
        grant_id: Optional[str] = None,
    ) -> Mapping[str, Any]:
        resolver = self._actor_execution_context_resolver
        if resolver is None:
            raise ManagedProviderCallbackError("managed actor execution authority is unavailable")
        try:
            context = resolver(
                session_id=identity["sessionID"],
                root_operation_id=identity["rootOperationID"],
                actor_id=identity["actorID"],
                execution_revision=identity["executionRevision"],
            )
            if inspect.isawaitable(context):
                context = await context
        except Exception as exc:
            raise ManagedProviderCallbackError("managed actor execution authority rejected the operation") from exc
        if not isinstance(context, Mapping) or (
            context.get("owner") != self._owner
            or context.get("sessionID") != identity.get("sessionID")
            or context.get("rootOperationID") != identity.get("rootOperationID")
            or context.get("actorID") != identity.get("actorID")
            or context.get("executionRevision") != identity.get("executionRevision")
            or not isinstance(context.get("effectiveModel"), Mapping)
            or (model_id is not None and context["effectiveModel"].get("modelID") != model_id)
        ):
            raise ManagedProviderCallbackError("managed actor execution identity changed")
        if model_route_id is not None:
            actor_model = context["effectiveModel"]
            try:
                resolved = await self.resolve_route_context(
                    f"{actor_model.get('providerID', '')}/{actor_model.get('modelID', '')}",
                    grant_id=grant_id,
                )
            except ManagedProviderRouteError as exc:
                raise ManagedProviderCallbackError("managed actor execution route is no longer authorized") from exc
            if (
                resolved.model_id != model_id
                or resolved.model_route_id != model_route_id
                or (provider_id is not None and resolved.provider_id != provider_id)
                or (connection_id is not None and resolved.connection_id != connection_id)
                or (billing_lane is not None and resolved.billing_lane != billing_lane)
            ):
                raise ManagedProviderCallbackError("managed actor execution escaped its provider route")
        return context

    def _binding_execution_identity(self, binding_id: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        from core.provider_models import ProviderOperationBinding
        db = self._session_factory()
        try:
            row = db.query(ProviderOperationBinding).filter_by(
                id=_required_text(binding_id, "binding ID"), owner=self._owner
            ).first()
            if row is None:
                raise ManagedProviderCallbackError("actor provider binding is unavailable")
            route = {"modelID": row.model_id, "providerID": row.provider_id,
                     "connectionID": row.connection_id, "billingLane": row.billing_lane,
                     "modelRouteID": row.model_route_id, "selectedAccountID": row.selected_account_id,
                     "credentialOwner": row.credential_owner, "grantID": row.grant_id,
                     "bindingRevision": row.revision, "credentialRevision": row.credential_revision,
                     "privateAuthority": (row.policy_snapshot or {}).get("private_authority")}
            if row.actor_id is not None:
                if not row.engine_session_id or row.execution_revision is None or not row.model_route_id:
                    raise ManagedProviderCallbackError("actor provider binding is incomplete")
                identity = {"scopeKind": "actor_execution", "sessionID": row.engine_session_id,
                            "rootOperationID": row.root_operation_id, "actorID": row.actor_id,
                            "executionRevision": int(row.execution_revision)}
            elif row.operation_id is not None:
                identity = {"scopeKind": "auxiliary_operation", "operationID": row.operation_id,
                            "rootOperationID": row.root_operation_id}
            else:
                raise ManagedProviderCallbackError("provider binding has no admitted execution scope")
            return identity, route
        finally:
            db.close()

    def _validate_prepared_auxiliary(self, identity: Mapping[str, Any], route: Mapping[str, Any]) -> None:
        """Recheck a frozen selection independently of optional capture state."""
        from src.openclank.chat_routing import resolve_chat_route, shared_endpoint_id
        from src.openclank.provider_request_options import project_request_options, validate_request_option_selection
        operation_id = identity.get("operationID")
        with self._logging_context_lock:
            if operation_id in self._retired_prepared_operations:
                raise ManagedProviderCallbackError("prepared operation has retired")
            entries = [entry for entry in self._prepared_operations.values() if entry.get("operation_id") == operation_id]
            if not entries:
                return  # Existing default-option operations have no prepared admission.
            if len(entries) != 1:
                raise ManagedProviderCallbackError("prepared operation authority is ambiguous")
            entry = entries[0]
            frozen_route, snapshot = entry["route"], entry["options"]
            if any(route.get(key) != value for key, value in frozen_route.items()):
                raise ManagedProviderCallbackError("prepared operation route changed")
        try:
            current = resolve_chat_route(owner=self._owner, model_route_id=frozen_route["modelRouteID"],
                endpoint_id=shared_endpoint_id(frozen_route["grantID"]) if frozen_route.get("grantID") else frozen_route["connectionID"],
                model_id=frozen_route["modelID"], provider_store=self._store)
            if (current.connection_id != frozen_route["connectionID"] or current.family_id != frozen_route["providerID"]
                or current.billing_lane != frozen_route["billingLane"] or current.provider_grant_id != frozen_route.get("grantID")):
                raise ValueError("route authority changed")
            actual = validate_request_option_selection(route_id=current.option_route_id, catalog_revision=current.catalog_revision,
                descriptors=project_request_options(current.capabilities, catalog_revision=current.catalog_revision, source="engine-catalog"),
                selection={"route_id": snapshot["optionRouteID"], "catalog_revision": snapshot["catalogRevision"], "options": snapshot["options"]})
            if actual != snapshot["options"]:
                raise ValueError("option selection changed")
        except Exception as exc:
            raise ManagedProviderCallbackError("prepared operation route or options changed; refresh required") from exc

    def _validate_auxiliary_operation(self, identity: Mapping[str, Any], route: Mapping[str, Any]) -> None:
        from core.operation_models import OperationJournal
        db = self._session_factory()
        try:
            journal = db.query(OperationJournal).filter_by(
                id=identity.get("operationID"), owner=self._owner,
                root_operation_id=identity.get("rootOperationID"),
            ).first()
            credential_owner = route.get("credentialOwner")
            if route.get("grantID"):
                share = db.query(ProviderShareGrant).filter_by(
                    id=route["grantID"], recipient=self._owner, state="active"
                ).first()
                if share is None:
                    raise ManagedProviderCallbackError("auxiliary operation share authority changed")
                credential_owner = share.owner
            model_route = db.query(ProviderModelRoute).filter_by(
                id=route.get("modelRouteID"), owner=credential_owner,
                provider_model_id=route.get("modelID"), enabled=True, deleted_at=None,
            ).first()
            connection = db.query(ProviderConnection).filter_by(
                id=route.get("connectionID"), owner=credential_owner,
                enabled=True, deleted_at=None,
            ).first()
            if (
                journal is None or journal.state in {"complete", "failed", "cancelled"}
                or journal.connection_id != route.get("connectionID")
                or journal.billing_lane != route.get("billingLane")
                or journal.model_route_id != route.get("modelRouteID")
                or model_route is None or connection is None
                or model_route.connection_id != connection.id
                or journal.operation not in set(model_route.operations or ())
                or connection.family_id != route.get("providerID")
            ):
                raise ManagedProviderCallbackError("auxiliary operation binding is no longer admitted")
            grant_id = route.get("grantID")
            if grant_id:
                grant = db.query(ProviderShareGrant).filter_by(
                    id=grant_id, owner=credential_owner,
                    recipient=self._owner, connection_id=connection.id, state="active",
                ).first()
                if (
                    grant is None
                    or grant.billing_lane != route.get("billingLane")
                    or not ModelSelector.parse(grant.model_selector).includes(model_route.id)
                    or (route.get("selectedAccountID") is not None
                        and not AccountSelector.parse(grant.account_selector).includes(route["selectedAccountID"]))
                ):
                    raise ManagedProviderCallbackError("auxiliary operation share authority changed")
        finally:
            db.close()

    async def _actor_route_preflight(self, params: Mapping[str, Any]) -> dict[str, Any]:
        resolver = self._actor_execution_context_resolver
        if resolver is None:
            raise ManagedProviderCallbackError("managed actor execution authority is unavailable")
        bridge=getattr(resolver,'__self__',None);original_root=None
        if params.get('actorID')=='main' and bridge is not None:
            if not bridge._provider_root_sync_guard.acquire(blocking=False):
                raise ManagedProviderCallbackError('provider root lifecycle unavailable')
            try:
                original_root=bridge._turns.get(params['sessionID'])
                if original_root is not None:
                    if original_root.root_operation_id!=params['rootOperationID'] or original_root.provider_root_closing:
                        raise ManagedProviderCallbackError('provider original root unavailable')
                else:
                    matches=[r for r in bridge._prepared_managed_handoffs.values()
                        if r.get('directMainPolicyIntent') is None and r.get('state')=='prepared'
                        and r.get('owner')==self._owner and r.get('sessionID')==params['sessionID']
                        and r.get('rootOperationID')==params['rootOperationID'] and r.get('actorID')=='main'
                        and r.get('expectedExecutionRevision')==params['expectedExecutionRevision']]
                    if len(matches)!=1: raise ManagedProviderCallbackError('provider original root unavailable')
            finally:
                bridge._provider_root_sync_guard.release()
        try:
            context = resolver(
                session_id=params["sessionID"],
                root_operation_id=params["rootOperationID"],
                actor_id=params["actorID"],
                execution_revision=params["expectedExecutionRevision"],
                require_running=False,
            )
            if inspect.isawaitable(context):
                context = await context
        except Exception as exc:
            raise ManagedProviderCallbackError("managed actor execution authority rejected preflight") from exc
        if not isinstance(context, Mapping):
            raise ManagedProviderCallbackError("managed actor execution authority returned invalid data")
        model = params.get("model")
        if not isinstance(model, Mapping):
            raise ManagedProviderCallbackError("managed actor model is invalid")
        if (
            context.get("owner") != self._owner
            or context.get("sessionID") != params.get("sessionID")
            or context.get("rootOperationID") != params.get("rootOperationID")
            or context.get("actorID") != params.get("actorID")
            or (params.get("parentActorID") is not None and context.get("parentActorID") != params.get("parentActorID"))
            or (params.get("expectedExecutionRevision") is not None and context.get("executionRevision") != params.get("expectedExecutionRevision"))
            or not isinstance(context.get("effectiveModel"), Mapping)
            or context["effectiveModel"].get("providerID") != model.get("providerID")
            or context["effectiveModel"].get("modelID") != model.get("modelID")
        ):
            raise ManagedProviderCallbackError("managed actor model or execution identity changed")

        runtime_model = f"{model.get('providerID', '')}/{model.get('modelID', '')}"
        try:
            route = await self.resolve_route_context(
                runtime_model,
                grant_id=params.get("grantID"),
            )
        except ManagedProviderRouteError as exc:
            raise ManagedProviderCallbackError("managed actor route is unavailable") from exc
        actor_model = context["effectiveModel"]
        option_snapshot = None
        candidate = params.get("optionCandidate")
        saved = params.get("savedOptionSnapshot")
        main_candidate = context.get("mainOptionCandidate")
        if params.get("actorID") == "main" and main_candidate is not None:
            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection, ProviderRequestOptionError
            if candidate is not None or saved is not None or not isinstance(main_candidate, Mapping):
                raise ManagedProviderCallbackError("main model option authority is ambiguous")
            if (main_candidate.get("provider_model_route_id") != route.model_route_id
                or main_candidate.get("provider_model_id") != route.model_id):
                raise ManagedProviderCallbackError("main model option route changed; refresh required")
            try:
                options = validate_request_option_selection(route_id=route.option_route_id, catalog_revision=route.catalog_revision,
                    descriptors=project_request_options(route.option_capabilities or {}, catalog_revision=route.catalog_revision, source="engine-catalog"),
                    selection=main_candidate)
            except ProviderRequestOptionError as exc:
                raise ManagedProviderCallbackError("main model options changed; refresh required") from exc
            option_snapshot = {"version": 1, "routeID": route.model_route_id, "optionRouteID": route.option_route_id,
                "catalogRevision": route.catalog_revision, "model": {"providerID": route.connection_id, "modelID": route.model_id},
                "options": options, "source": {"kind": "receipt", "requestID": params["rootOperationID"]}}
        if candidate is not None:
            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection, ProviderRequestOptionError
            source = candidate["source"]
            if source.get("kind") == "parent_execution":
                if (any(source.get(k) is None for k in ("sessionID", "actorID", "executionRevision", "rootOperationID", "messageID"))
                    or source["rootOperationID"] != params["rootOperationID"] or source["actorID"] != params.get("parentActorID")):
                    raise ManagedProviderCallbackError("parent option provenance is invalid")
                lineage = context.get("lineage") or []
                if len(lineage) < 2 or any(lineage[1].get(k) != source.get(k) for k in ("sessionID", "actorID", "executionRevision")):
                    raise ManagedProviderCallbackError("parent option provenance does not match immediate lineage")
                parent = await self._validate_actor_execution(source, model_id=candidate["model"]["modelID"])
                if parent["effectiveModel"].get("providerID") != candidate["model"]["providerID"]:
                    raise ManagedProviderCallbackError("parent option model changed")
                same_model = candidate["model"] == {"providerID": route.connection_id, "modelID": route.model_id}
                variant = candidate["variant"] if same_model else None
            elif source == {"kind":"default"} and candidate["variant"] is None:
                same_model = False
                variant = None
            else:
                raise ManagedProviderCallbackError("child default option provenance is invalid")
            descriptors = project_request_options(route.option_capabilities or {}, catalog_revision=route.catalog_revision, source="engine-catalog")
            selection = {"route_id": route.option_route_id, "catalog_revision": route.catalog_revision,
                         "options": ({"reasoning_effort": variant} if variant is not None else {})}
            try:
                options = validate_request_option_selection(route_id=route.option_route_id, catalog_revision=route.catalog_revision,
                    descriptors=descriptors, selection=selection)
            except ProviderRequestOptionError as exc:
                raise ManagedProviderCallbackError("child model options require refresh") from exc
            option_snapshot = {"version":1, "routeID":route.model_route_id, "optionRouteID":route.option_route_id,
                "catalogRevision":route.catalog_revision, "model":{"providerID":route.connection_id, "modelID":route.model_id},
                "options":options, "source":dict(source) if same_model else {"kind":"default"}}
        if saved is not None:
            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection, ProviderRequestOptionError
            if saved != context.get("modelRequestOptions"):
                raise ManagedProviderCallbackError("saved child model options do not match actor execution")
            if (saved.get("routeID") != route.model_route_id or saved.get("model") != {"providerID":route.connection_id, "modelID":route.model_id}):
                raise ManagedProviderCallbackError("saved child model options changed route; refresh required")
            try:
                options = validate_request_option_selection(route_id=route.option_route_id, catalog_revision=route.catalog_revision,
                    descriptors=project_request_options(route.option_capabilities or {}, catalog_revision=route.catalog_revision, source="engine-catalog"),
                    selection={"route_id":saved.get("optionRouteID"), "catalog_revision":saved.get("catalogRevision"), "options":saved.get("options")})
            except ProviderRequestOptionError as exc:
                raise ManagedProviderCallbackError("saved child model options require refresh") from exc
            option_snapshot = {**saved, "options":options}
        if actor_model.get("providerID") not in {route.connection_id, route.provider_id}:
            raise ManagedProviderCallbackError("managed actor model resolved to a different provider route")
        # Direct MAIN preflight reserves its original selected/Off decision in
        # the existing host handoff owner before the bootstrap leaves the host.
        if params.get('actorID') == 'main' and context.get('sessionID') == context.get('rootSessionID') and not context.get('authority'):
            reserve=getattr(bridge,'reserve_direct_main_policy_preflight',None)
            if original_root is not None:
                if not callable(reserve): raise ManagedProviderCallbackError('provider preflight owner unavailable')
                await reserve(dict(context),params['expectedExecutionRevision'],original_root)
        return {
            "applicability": {"version":1, "methodPolicy":"required" if route.method_policy is not None else "not_applicable",
                              "grant":"shared" if route.grant_id is not None else "none"},
            **({"modelRequestOptions": option_snapshot} if option_snapshot is not None else {}),
            "bootstrap": {
                **{key: context[key] for key in ("stableChatID", "owner", "rootSessionID", "sessionID", "actorID", "rootOperationID")},
                **({"authority": context["authority"]} if context.get("authority") else {key: context[key] for key in ("mapRevision", "mappingRevision", "workspaceRevision")}),
                "expectedExecutionRevision": params["expectedExecutionRevision"],
                "modelRouteID": route.model_route_id,
                **({"modelRequestOptions": option_snapshot} if option_snapshot is not None else {}),
                **({"handoffID": context["handoffID"]} if context.get("handoffID") else {}),
            },
            "connectionID": route.connection_id,
            "providerID": route.provider_id,
            "billingLane": route.billing_lane,
            "modelID": route.model_id,
            "modelRouteID": route.model_route_id,
            **({"methodPolicy": route.method_policy.to_wire()} if route.method_policy else {}),
            **({"grantID": route.grant_id} if route.grant_id else {}),
            **({"grantRevision": route.grant_revision} if route.grant_revision is not None else {}),
        }

    def set_search_delegate(self, delegate) -> None:
        """Install the non-recursive host prepare/execute/finalize delegate."""
        if not callable(delegate):
            raise ValueError("managed search delegate is required")
        self._search_delegate = delegate

    def close_search_handoffs(self) -> None:
        """Drop generation-bound search state during worker teardown."""
        with self._search_handoff_lock:
            self._search_closed = True
            for entry in self._search_handoffs.values():
                if entry.timer is not None:
                    entry.timer.cancel()
                if entry.dispatch_task is not None and not entry.dispatch_task.done():
                    entry.dispatch_task.cancel()
                future = entry.dispatch_future
                if future is not None and not future.done():
                    future.set_exception(ManagedProviderCallbackError("managed search generation closed"))
                    future.exception()
            self._search_handoffs.clear()
            self._search_dispatches.clear()
            self._search_dispatch_turns.clear()
            self._search_expired.clear()
            self._search_cancelled.clear()
            self._search_cancelled_turns.clear()

    def retire_search_turn(self, session_id: str, root_operation_id: str) -> None:
        """Release all transient search identities for a completed parent turn."""
        with self._search_handoff_lock:
            for handoff_id, entry in list(self._search_handoffs.items()):
                if entry.session_id == session_id and entry.root_operation_id == root_operation_id:
                    if entry.timer is not None:
                        entry.timer.cancel()
                    if entry.dispatch_task is not None and not entry.dispatch_task.done():
                        entry.dispatch_task.cancel()
                    self._search_handoffs.pop(handoff_id, None)
                    self._search_dispatches.pop(entry.dispatch_key, None)
                    self._search_expired.discard(entry.dispatch_key)
                    self._search_cancelled.discard(entry.dispatch_key)
                    self._search_cancelled_turns.pop(entry.dispatch_key, None)
                    self._search_dispatch_turns.pop(entry.dispatch_key, None)
                    future = entry.dispatch_future
                    if future is not None and not future.done():
                        future.set_exception(ManagedProviderCallbackError("managed search turn retired"))
                        future.exception()
            for dispatch_key, identity in list(self._search_dispatch_turns.items()):
                if identity == (session_id, root_operation_id):
                    self._search_dispatch_turns.pop(dispatch_key, None)
                    self._search_dispatches.pop(dispatch_key, None)
                    self._search_expired.discard(dispatch_key)
                    self._search_cancelled.discard(dispatch_key)
                    self._search_cancelled_turns.pop(dispatch_key, None)
            for dispatch_key, identity in list(self._search_cancelled_turns.items()):
                if identity == (session_id, root_operation_id):
                    self._search_cancelled_turns.pop(dispatch_key, None)
                    self._search_cancelled.discard(dispatch_key)

    def _cleanup_search_handoffs(self, now: Optional[float] = None) -> None:
        current = self._search_clock() if now is None else now
        with self._search_handoff_lock:
            for handoff_id, entry in list(self._search_handoffs.items()):
                if entry.deadline <= current:
                    if entry.timer is not None:
                        entry.timer.cancel()
                    if entry.dispatch_future is not None and not entry.dispatch_future.done():
                        entry.failure = ManagedProviderCallbackError("managed search deadline expired")
                        entry.dispatch_future.set_exception(entry.failure)
                        entry.dispatch_future.exception()
                    self._search_handoffs.pop(handoff_id, None)
                    self._search_expired.add(entry.dispatch_key)

    def _expire_search_handoff(self, handoff_id: str) -> None:
        with self._search_handoff_lock:
            entry = self._search_handoffs.pop(handoff_id, None)
            if entry is None:
                return
            self._search_expired.add(entry.dispatch_key)
            entry.failure = ManagedProviderCallbackError("managed search deadline expired")
            if entry.dispatch_future is not None and not entry.dispatch_future.done():
                entry.dispatch_future.set_exception(entry.failure)
                entry.dispatch_future.exception()

    def _fail_search_dispatch(self, dispatch_key: str, handoff_id: str, future: Any, error: BaseException) -> None:
        with self._search_handoff_lock:
            entry = self._search_handoffs.get(handoff_id)
            if entry is not None:
                entry.failure = error
                if entry.timer is not None:
                    entry.timer.cancel()
            if not future.done():
                future.set_exception(error)
                future.exception()

    async def _search_delegate_call(self, **kwargs):
        if self._search_delegate is None:
            raise ManagedProviderCallbackError("managed search host delegate is unavailable")
        result = self._search_delegate(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise ManagedProviderCallbackError("managed search host delegate returned invalid data")
        return dict(result)

    @staticmethod
    def _search_claim_fingerprint(claims: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(dict(claims), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    async def _search_artifact(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            from src.openclank.managed_protocol import _validated_contract
            contract, validator_type = _validated_contract()
            validator = validator_type(contract)
            error = next(validator.descend(dict(payload), contract["$defs"]["searchArtifactResult"]), None)
            if error is not None:
                raise ManagedProviderCallbackError("managed search artifact failed canonical validation")
            encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except ManagedProviderCallbackError:
            raise
        except Exception as exc:
            raise ManagedProviderCallbackError("managed search artifact validation is unavailable") from exc
        if len(encoded) > 16 * 1024 * 1024:
            raise ManagedProviderCallbackError("managed search artifact is too large")
        row = self._artifact_store.put(
            owner=self._owner,
            chunks=(encoded,),
            media_type="application/json",
        )
        return self._artifact_descriptor(row)

    async def _search_callback(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("artifactIDs"):
            raise ManagedProviderCallbackError("managed search does not accept input artifacts")
        if self._search_context_resolver is None:
            raise ManagedProviderCallbackError("managed search session authority is unavailable")
        claims = params["options"]["claims"]
        context = self._search_context_resolver(
            claims["sessionID"], claims["rootOperationID"],
        )
        if inspect.isawaitable(context):
            context = await context
        if not isinstance(context, Mapping):
            raise ManagedProviderCallbackError("managed search session authority is invalid")
        if str(context.get("owner") or "").strip().lower() != self._owner:
            raise ManagedProviderCallbackError("managed search owner escaped callback authority")
        if str(context.get("holderID") or "") != self._holder_id:
            raise ManagedProviderCallbackError("managed search generation is stale")
        if str(context.get("sessionID") or "") != claims["sessionID"]:
            raise ManagedProviderCallbackError("managed search session escaped callback authority")
        if str(context.get("rootOperationID") or "") != claims["rootOperationID"]:
            raise ManagedProviderCallbackError("managed search root escaped callback authority")
        self._cleanup_search_handoffs()
        if self._search_closed:
            raise ManagedProviderCallbackError("managed search generation is closed")
        action = params["options"]["action"]
        fingerprint = self._search_claim_fingerprint(claims)
        if action == "dispatch":
            input_fingerprint = hashlib.sha256(
                json.dumps(params["options"]["input"], sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            dispatch_key = fingerprint
            with self._search_handoff_lock:
                existing_id = self._search_dispatches.get(dispatch_key)
                existing = self._search_handoffs.get(existing_id) if existing_id else None
                if dispatch_key in self._search_expired or dispatch_key in self._search_cancelled:
                    raise ManagedProviderCallbackError("managed search dispatch has expired")
                if existing is not None:
                    if existing.input_fingerprint != input_fingerprint:
                        raise ManagedProviderCallbackError("managed search claims were reused with different input")
                    if existing.dispatch_deadline and existing.dispatch_deadline <= self._search_clock():
                        raise ManagedProviderCallbackError("managed search dispatch deadline expired")
                    future = existing.dispatch_future
                    is_new = False
                elif len(self._search_dispatches) >= self._max_search_handoffs:
                    raise ManagedProviderCallbackError("managed search handoff capacity is exhausted")
                else:
                    future = asyncio.get_running_loop().create_future()
                    handoff_id = f"sh_{secrets.token_urlsafe(24)}"
                    existing = _SearchHandoff(
                        dispatch_key=dispatch_key, fingerprint=fingerprint,
                        input_fingerprint=input_fingerprint, deadline=self._search_clock() + 120.0,
                        session_id=claims["sessionID"], root_operation_id=claims["rootOperationID"],
                        prepared=None, wire={}, dispatch_future=future,
                        terminal_lock=asyncio.Lock(),
                    )
                    self._search_handoffs[handoff_id] = existing
                    self._search_dispatches[dispatch_key] = handoff_id
                    self._search_dispatch_turns[dispatch_key] = (
                        claims["sessionID"], claims["rootOperationID"],
                    )
                    existing_id = handoff_id
                    is_new = True
            if not is_new:
                if existing.failure is not None:
                    raise existing.failure
                return await asyncio.shield(future)
            now = self._search_clock()
            deadline_at = float(params["options"]["input"]["deadlineAt"])
            remaining = deadline_at / 1000.0 - time.time()
            if remaining <= 0:
                with self._search_handoff_lock:
                    existing.failure = ManagedProviderCallbackError("managed search deadline expired")
                    self._search_handoffs.pop(existing_id, None)
                    self._search_expired.add(dispatch_key)
                    if not future.done():
                        future.set_exception(existing.failure)
                        future.exception()
                raise ManagedProviderCallbackError("managed search deadline expired")
            dispatch_deadline = now + min(120.0, remaining)
            existing.dispatch_deadline = dispatch_deadline
            # Keep a bounded settlement window so the engine can reconcile a
            # terminal receipt after its execution deadline without admitting
            # a new dispatch for the expired request.
            deadline = dispatch_deadline + 30.0
            existing.deadline = deadline
            existing.timer = asyncio.get_running_loop().call_later(
                max(0.0, deadline - now), self._expire_search_handoff, existing_id,
            )
            child_root = f'search-{hashlib.sha256((claims["rootOperationID"] + "\0" + claims["correlationID"]).encode()).hexdigest()[:48]}'
            try:
                existing.dispatch_task = asyncio.create_task(self._search_delegate_call(
                    action="dispatch", context=dict(context), claims=dict(claims),
                    input=dict(params["options"]["input"]), child_root_operation_id=child_root,
                    child_idempotency_key=claims["idempotencyKey"],
                ))
                try:
                    result = await asyncio.shield(existing.dispatch_task)
                except asyncio.CancelledError:
                    # Cancellation of the delegate task is an external
                    # abort, not cancellation of this callback's receipt.
                    if existing.dispatch_task.cancelled():
                        return await asyncio.shield(future)
                    existing.dispatch_task.cancel()
                    error = ManagedProviderCallbackError("managed search callback cancelled")
                    self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                    raise
            except Exception as error:
                self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                raise
            with self._search_handoff_lock:
                if self._search_closed or self._search_handoffs.get(existing_id) is not existing or dispatch_key in self._search_expired:
                    error = ManagedProviderCallbackError("managed search handoff expired")
                    self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                    raise error
            kind = str(result.get("kind") or "")
            if kind == "finalized":
                try:
                    artifact = await self._search_artifact({
                        "kind": kind, "response": result.get("response") or {},
                    })
                except Exception as error:
                    self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                    raise
                existing.artifact = artifact
                if not future.done(): future.set_result(artifact)
                return artifact
            if kind == "external_result":
                try:
                    artifact = await self._search_artifact({"kind": kind, "response": result.get("response") or {}})
                except Exception as error:
                    self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                    raise
                existing.artifact = artifact
                if not future.done(): future.set_result(artifact)
                return artifact
            if kind != "managed_request" or not isinstance(result.get("preparedRequest"), Mapping):
                error = ManagedProviderCallbackError("managed search dispatch returned invalid kind")
                self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                raise error
            existing.prepared = result.get("prepared")
            existing.wire = dict(result["preparedRequest"])
            try:
                artifact = await self._search_artifact({
                    "kind": kind, "handoffID": existing_id,
                    "request": dict(result["preparedRequest"]),
                })
            except Exception as error:
                self._fail_search_dispatch(dispatch_key, existing_id, future, error)
                raise
            existing.artifact = artifact
            if not future.done(): future.set_result(artifact)
            return artifact
        requested_handoff_id = str(params["options"].get("handoffID") or "")
        with self._search_handoff_lock:
            effective_handoff_id = requested_handoff_id or self._search_dispatches.get(fingerprint, "")
            entry = self._search_handoffs.get(effective_handoff_id)
        if action == "cancel" and requested_handoff_id and (entry is None or entry.fingerprint != fingerprint):
            raise ManagedProviderCallbackError("managed search handoff is unavailable")
        if action == "cancel" and (entry is None or entry.fingerprint != fingerprint):
            # A cancel racing ahead of dispatch is a claims-only tombstone.
            # It is retained until the active parent turn retires, preventing
            # a later retry from starting paid external work.
            with self._search_handoff_lock:
                if fingerprint not in self._search_cancelled and len(self._search_dispatches) >= self._max_search_handoffs:
                    raise ManagedProviderCallbackError("managed search handoff capacity is exhausted")
                self._search_dispatches[fingerprint] = ""
            self._search_cancelled.add(fingerprint)
            self._search_cancelled_turns[fingerprint] = (
                claims["sessionID"], claims["rootOperationID"],
            )
            return await self._search_artifact({
                "kind": "cancelled",
                "reason": params["options"].get("reason") or "aborted",
            })
        if entry is None or entry.fingerprint != fingerprint:
            raise ManagedProviderCallbackError("managed search handoff is unavailable")
        terminal = params["options"].get("terminal")
        if action == "finalize":
            async with entry.terminal_lock:
                raw = json.dumps(terminal, sort_keys=True, separators=(",", ":"))
                if entry.receipt_raw is not None:
                    if entry.receipt_raw != raw:
                        raise ManagedProviderCallbackError("conflicting managed search finalization")
                    return await self._search_artifact(entry.receipt or {})
                result = await self._search_delegate_call(
                    action="finalize", context=dict(context), claims=dict(claims),
                    prepared=entry.prepared, terminal=dict(terminal),
                )
                entry.receipt_raw = raw
                entry.receipt = dict(result)
                return await self._search_artifact(result)
        if action == "cancel":
            async with entry.terminal_lock:
                if entry.receipt is not None:
                    return await self._search_artifact(entry.receipt)
                task = entry.dispatch_task
                if task is not None and not task.done() and task is not asyncio.current_task():
                    task.cancel()
                    try:
                        await asyncio.shield(task)
                    except BaseException:
                        pass
                if entry.artifact is not None and entry.prepared is None:
                    # An already-completed external/early response wins over
                    # a late cancel; the descriptor is already canonical.
                    return entry.artifact
                result = await self._search_delegate_call(
                    action="cancel", context=dict(context), claims=dict(claims),
                    prepared=entry.prepared, reason=params["options"].get("reason"),
                )
                entry.receipt_raw = "cancel:" + str(params["options"].get("reason") or "aborted")
                entry.receipt = dict(result)
                artifact = await self._search_artifact(result)
                entry.artifact = artifact
                if entry.dispatch_future is not None and not entry.dispatch_future.done():
                    entry.dispatch_future.set_result(artifact)
                return artifact
        raise ManagedProviderCallbackError("managed search action is unsupported")

    @staticmethod
    def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
        if depth > 12:
            raise ManagedProviderCallbackError(
                "managed operation metadata is too deeply nested"
            )
        if isinstance(value, Mapping):
            result = {}
            for key, child in value.items():
                name = str(key)
                if name.strip().lower() in _SENSITIVE_METADATA_KEYS:
                    raise ManagedProviderCallbackError(
                        "managed operation metadata contains a forbidden field"
                    )
                result[name] = ManagedProviderCallbacks._safe_metadata(
                    child,
                    depth=depth + 1,
                )
            return result
        if isinstance(value, list):
            return [
                ManagedProviderCallbacks._safe_metadata(item, depth=depth + 1)
                for item in value
            ]
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        raise ManagedProviderCallbackError(
            "managed operation metadata is not JSON-safe"
        )

    @staticmethod
    def _epoch_milliseconds(value) -> Optional[int]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)

    def _artifact_descriptor(self, row) -> dict[str, Any]:
        result: dict[str, Any] = {
            "artifactID": row.id,
            "contentSHA256": row.content_sha256,
            "sizeBytes": int(row.size_bytes),
            "mediaType": row.media_type,
            "state": row.state,
        }
        expires_at = self._epoch_milliseconds(row.expires_at)
        if expires_at is not None:
            result["expiresAt"] = expires_at
        return result

    @staticmethod
    def _journal_result(row, *, replayed: bool) -> dict[str, Any]:
        result: dict[str, Any] = {
            "operationID": row.id,
            "rootOperationID": row.root_operation_id,
            "operation": row.operation,
            "requestHash": row.request_hash,
            "connectionID": row.connection_id,
            "billingLane": row.billing_lane,
            "modelRouteID": row.model_route_id,
            "state": row.state,
            "committed": bool(row.committed),
            "attempts": list(row.attempts or ()),
            "artifactIDs": list(row.artifact_ids or ()),
            "revision": int(row.revision),
            "replayed": bool(replayed),
        }
        for attribute, key in (
            ("binding_id", "bindingID"),
            ("selected_account_id", "selectedAccountID"),
            ("commit_reason", "commitReason"),
        ):
            value = getattr(row, attribute, None)
            if value:
                result[key] = value
        return result

    def _journal_callback(self, params: Mapping[str, Any]) -> dict[str, Any]:
        action = params.get("action")
        if action == "begin":
            request = self._safe_metadata(params.get("request"))
            key = (str(params["rootOperationID"]), str(params["idempotencyKey"]))
            with self._generation_lock:
                if self._generation_closed.is_set():
                    raise ManagedProviderCallbackError("managed provider generation is closed")
                with self._logging_context_lock:
                    prepared = self._prepared_operations.get(key)
                    request_options = request.get("options")
                    if isinstance(request_options, Mapping) and "modelRequestOptions" in request_options and prepared is None:
                        raise ManagedProviderCallbackError("prepared model option authority is unavailable")
                    if prepared is not None:
                        if canonical_request_hash(request) != prepared["request_hash"]:
                            raise ManagedProviderCallbackError("prepared operation request changed")
                        options = request.get("options")
                        routes = request.get("routes")
                        if (not isinstance(options, Mapping) or options.get("modelRequestOptions") != prepared["options"]
                            or not isinstance(routes, list) or len(routes) != 1
                            or not isinstance(routes[0], Mapping)
                            or any(routes[0].get(key) != value for key, value in prepared["route"].items())
                            or routes[0].get("grantID") != prepared["route"].get("grantID")):
                            raise ManagedProviderCallbackError("prepared operation options or route changed")
                row, replayed = self._operation_journal.begin(
                    owner=self._owner,
                    root_operation_id=str(params["rootOperationID"]),
                    operation=str(params["operation"]),
                    idempotency_key=str(params["idempotencyKey"]),
                    request=request,
                    connection_id=str(params["connectionID"]),
                    billing_lane=str(params["billingLane"]),
                    model_route_id=str(params["modelRouteID"]),
                )
                with self._logging_context_lock:
                    context = self._logging_contexts.get(row.root_operation_id)
                    if context is not None:
                        # Preserve the existing journal propagation and retain an
                        # operation-keyed association when roots contain multiple operations.
                        context["operation_id"] = row.id
                        self._logging_operations[row.id] = dict(context)
                with self._logging_context_lock:
                    if prepared is not None:
                        if prepared["operation_id"] not in (None, row.id):
                            raise ManagedProviderCallbackError("prepared operation journal changed")
                        prepared["operation_id"] = row.id
                        context = self._logging_operations.setdefault(row.id, {
                            "owner": self._owner, "holder_id": self._holder_id,
                            "root_operation_id": row.root_operation_id, "operation_id": row.id,
                            "prepared_only": True,
                        })
                        context["prepared_registration"] = prepared["registration"]
                        context["prepared_options"] = prepared["options"]
            return self._journal_result(row, replayed=replayed)
        if action == "cas":
            attempt = params.get("attempt")
            row = self._operation_journal.cas(
                owner=self._owner,
                operation_id=str(params["operationID"]),
                expected_revision=int(params["expectedRevision"]),
                state=params.get("state"),
                binding_id=params.get("bindingID"),
                selected_account_id=params.get("selectedAccountID"),
                attempt=(
                    self._safe_metadata(attempt)
                    if attempt is not None
                    else None
                ),
                commit_reason=params.get("commitReason"),
                artifact_id=params.get("artifactID"),
            )
            return self._journal_result(row, replayed=False)
        raise ManagedProviderCallbackError(
            "managed operation journal action is unsupported"
        )

    def _artifact_write_callback(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("action") == "acknowledge":
            row = self._artifact_store.acknowledge(
                owner=self._owner,
                artifact_id=str(params["artifactID"]),
            )
            return self._artifact_descriptor(row)
        if params.get("action") != "put":
            raise ManagedProviderCallbackError(
                "managed artifact write action is unsupported"
            )
        chunks: list[bytes] = []
        expected_index = 0
        digest = hashlib.sha256()
        total = 0
        for item in params.get("chunks") or ():
            if int(item["index"]) != expected_index:
                raise ManagedProviderCallbackError(
                    "managed artifact chunks are not contiguous"
                )
            expected_index += 1
            try:
                chunk = base64.b64decode(item["dataBase64"], validate=True)
            except (binascii.Error, ValueError, TypeError):
                raise ManagedProviderCallbackError(
                    "managed artifact chunk is not valid base64"
                ) from None
            if len(chunk) > 8 * 1024 * 1024:
                raise ManagedProviderCallbackError(
                    "managed artifact chunk exceeds 8 MiB"
                )
            chunk_hash = hashlib.sha256(chunk).hexdigest()
            if chunk_hash != item["sha256"]:
                raise ManagedProviderCallbackError(
                    "managed artifact chunk failed its content hash"
                )
            total += len(chunk)
            digest.update(chunk)
            chunks.append(chunk)
        if total != int(params["sizeBytes"]):
            raise ManagedProviderCallbackError(
                "managed artifact size does not match its declaration"
            )
        if digest.hexdigest() != params["contentSHA256"]:
            raise ManagedProviderCallbackError(
                "managed artifact failed its content hash"
            )
        row = self._artifact_store.put(
            owner=self._owner,
            chunks=chunks,
            media_type=str(params["mediaType"]),
        )
        return self._artifact_descriptor(row)

    def _artifact_read_callback(self, params: Mapping[str, Any]) -> dict[str, Any]:
        artifact_id = str(params["artifactID"])
        offset = int(params["offset"])
        limit = int(params["limit"])
        row = self._artifact_store.get(owner=self._owner, artifact_id=artifact_id)
        if offset > int(row.size_bytes):
            raise ManagedProviderCallbackError(
                "managed artifact offset exceeds its size"
            )
        selected: list[bytes] = []
        position = 0
        end = min(int(row.size_bytes), offset + limit)
        # Exhaust the iterator even after the requested range so ArtifactStore
        # verifies the complete content hash before bytes cross the boundary.
        for chunk in self._artifact_store.read_chunks(
            owner=self._owner,
            artifact_id=artifact_id,
        ):
            chunk_end = position + len(chunk)
            if chunk_end > offset and position < end:
                left = max(0, offset - position)
                right = min(len(chunk), end - position)
                selected.append(chunk[left:right])
            position = chunk_end
        data = b"".join(selected)
        return {
            "artifactID": row.id,
            "contentSHA256": row.content_sha256,
            "sizeBytes": int(row.size_bytes),
            "mediaType": row.media_type,
            "offset": offset,
            "dataBase64": base64.b64encode(data).decode("ascii"),
            "chunkSHA256": hashlib.sha256(data).hexdigest(),
            "eof": end >= int(row.size_bytes),
        }

    async def _executor_callback(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params.get("executorID") == "openclank.search.v1":
            return await self._search_callback(params)
        if self._executor_broker is None:
            raise ManagedProviderCallbackError(
                "managed local executor broker is unavailable"
            )
        row = await self._executor_broker.invoke(
            owner=self._owner,
            executor_id=str(params["executorID"]),
            operation=str(params["operation"]),
            artifact_ids=[str(value) for value in params["artifactIDs"]],
            options=self._safe_metadata(params.get("options") or {}),
        )
        return self._artifact_descriptor(row)

    def register_logging_operation(self, root_operation_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
        """Supervisor/bridge-only admission; never an engine owner selector."""
        trusted = {**dict(context), "owner": self._owner, "holder_id": self._holder_id,
                   "root_operation_id": root_operation_id, "operation_id": context.get("operation_id")}
        if trusted.get("operation_type") == "chat" and trusted.get("connection_id") and trusted.get("route_id"):
            journal, _ = self._operation_journal.begin(owner=self._owner, root_operation_id=root_operation_id,
                operation="chat.complete", idempotency_key="chat_turn_"+root_operation_id,
                request={"kind": "chat.turn", "root": root_operation_id,
                         "route": trusted["route_id"], "model": trusted.get("model_id")},
                connection_id=trusted["connection_id"], billing_lane=trusted.get("billing_lane") or "local",
                model_route_id=trusted["route_id"])
            trusted["operation_id"] = journal.id
            trusted["chat_journal"] = True
        self._capture.policy.pin_policy(self._owner, root_operation_id, holder_id=self._holder_id,
                                        incognito=bool(trusted.get("incognito")))
        with self._logging_context_lock:
            if len(self._logging_contexts) >= 1000 and root_operation_id not in self._logging_contexts:
                raise ManagedProviderCallbackError("logging operation capacity unavailable")
            self._logging_contexts[root_operation_id] = trusted
            if trusted["operation_id"]:
                self._logging_operations[trusted["operation_id"]] = dict(trusted)
        return {"operationID": trusted["operation_id"], "identityCoverage": "complete" if trusted["operation_id"] else "partial"}

    def record_logging_metrics(self, root_operation_id: str, metrics: Mapping[str, Any], outcome: str = "completed", *, alternative_attribution=None) -> bool:
        """Persist actual final usage through the active trusted journal context."""
        with self._logging_context_lock:
            context = dict(self._logging_contexts.get(root_operation_id) or {})
            if context.get("root_operation_id") != root_operation_id or not context.get("operation_id"):
                raise ManagedProviderCallbackError("chat usage operation is inactive")
            if context.get("incognito") or context.get("temporary") or self._capture.policy.private_operation(self._owner, root_operation_id):
                return False
            from core.operation_models import OperationJournal
            from services.stats.ledger import capture_chat_metrics
            db = self._session_factory()
            try:
                journal = db.query(OperationJournal).filter_by(owner=self._owner, id=context["operation_id"], root_operation_id=root_operation_id).first()
                if journal is None:
                    raise ManagedProviderCallbackError("chat usage journal is foreign")
                from core.provider_models import ProviderOperationBinding
                binding = db.query(ProviderOperationBinding).filter_by(owner=self._owner, root_operation_id=root_operation_id, connection_id=context.get("connection_id"), model_id=context.get("model_id")).first()
                if binding is not None:
                    context.update(provider_id=binding.provider_id, account_id=binding.selected_account_id)
                metrics = dict(metrics)
                metrics.pop("actual_model_state", None)
                metrics.pop("actual_model_options", None)
                if alternative_attribution is not None:
                    from src.openclank.provider_alternative_proof import AlternativeAttribution
                    if type(alternative_attribution) is not AlternativeAttribution or alternative_attribution.state not in ('unknown', 'verified_alternative'):
                        raise ManagedProviderCallbackError('alternative attribution unavailable')
                    # The trusted in-process verifier, not raw ACP metadata, owns this override.
                    context.update(provider_id=alternative_attribution.provider_id, account_id=None,
                                   route_id=alternative_attribution.route_id, connection_id=alternative_attribution.connection_id)
                    metrics['actual_model_state'] = alternative_attribution.state
                    metrics['actual_model'] = alternative_attribution.model
                    if alternative_attribution.state == 'verified_alternative':
                        metrics['actual_model_options'] = dict(alternative_attribution.thinking)
                context["instance_id"] = self._holder_id
                capture_chat_metrics(db, owner=self._owner, context=context, metrics=metrics, outcome=outcome)
                db.commit()
                return True
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def finish_logging_operation(self, root_operation_id: str, outcome: str | None = None) -> None:
        with self._logging_context_lock:
            context = dict(self._logging_contexts.get(root_operation_id) or {})
        try:
            self._capture.finish_operation(self._owner, self._holder_id, root_operation_id)
            if context.get("chat_journal") and outcome:
                from core.operation_models import OperationJournal
                db = self._session_factory()
                try:
                    row = db.query(OperationJournal).filter_by(owner=self._owner, id=context["operation_id"]).first()
                    if row is not None and row.state not in {"complete", "failed", "cancelled"}:
                        self._operation_journal.cas(owner=self._owner, operation_id=row.id, expected_revision=row.revision,
                            state="complete" if outcome == "completed" else "cancelled" if outcome == "cancelled" else "failed",
                            commit_reason="chat_turn_complete" if outcome == "completed" else None)
                finally:
                    db.close()
        finally:
            with self._logging_context_lock:
                self._logging_contexts.pop(root_operation_id, None)
                self._logging_operations = {key: value for key, value in self._logging_operations.items()
                    if value["root_operation_id"] != root_operation_id or value.get("prepared_registration")}

    def teardown_logging(self) -> None:
        try:
            self._capture.interrupt_holder(self._owner, self._holder_id)
        finally:
            with self._logging_context_lock:
                self._logging_contexts.clear()
                self._logging_operations.clear()

    def finish_actor_logging(self, identity: Mapping[str, Any], evidence=None) -> str:
        exact = {key:identity[key] for key in ('sessionID','rootOperationID','actorID','executionRevision')}
        state = 'absent' if evidence is None else 'unavailable'
        try:
            if evidence is not None:
                state = self._record_actor_final(exact,evidence)
        except Exception:
            state = 'unavailable'
        finally:
            self._capture.finish_actor_execution(self._owner,self._holder_id,exact)
        return state

    def _record_actor_final(self, identity, evidence):
        from core.provider_models import ProviderOperationBinding
        from services.stats.ledger import capture_chat_metrics, TOKEN_FIELDS, EXTRA_METRICS, _metric, _nonnegative
        context = self._capture.actor_final_context(self._owner,self._holder_id,identity,
                                                    evidence['bindingID'],evidence['bindingRevision'])
        if not context:
            return 'unavailable'
        db = self._session_factory()
        try:
            row = db.query(ProviderOperationBinding).filter_by(owner=self._owner,id=evidence['bindingID'],
                root_operation_id=identity['rootOperationID'],engine_session_id=identity['sessionID'],
                actor_id=identity['actorID'],execution_revision=identity['executionRevision'],revision=evidence['bindingRevision']).first()
            if (row is None or row.connection_id != context.get('connection_id')
                or row.model_route_id != context.get('binding_route_id') or row.selected_account_id != context.get('account_id')):
                return 'unavailable'
            if context.get('actor_kind') == 'foreground':
                receipt = evidence.get('lastDispatch')
                if not receipt or type(context.get('mapping_revision')) is not int:
                    return 'unavailable'
                from services.stats.ledger import capture_context_receipt
                capture_context_receipt(db, owner=self._owner, context=context, identity=identity, binding=row, receipt=receipt)
                db.commit()
                return 'recorded'
            metrics = {**evidence['usage'], 'metric_coverage':evidence['metricCoverage'],
                'covered_dispatch_ids':evidence['coveredDispatchIDs'],'loss_reasons':evidence['lossReasons'],
                'identity_coverage':evidence['identityCoverage'],'normalization_profile':evidence['normalizationProfile']}
            outcome = {'success':'completed','failure':'failure','cancelled':'cancelled'}[evidence['outcome']]
            event = capture_chat_metrics(db,owner=self._owner,context={**context, 'owner': self._owner},metrics=metrics,outcome=outcome)
            def expected_metric(key):
                coverage = evidence['metricCoverage'].get(key) or {}
                return None if coverage.get('state') in ('unavailable','not_applicable') else _nonnegative(_metric(metrics,key))
            if (event is None or any(getattr(event,key) != expected_metric(key) for key in TOKEN_FIELDS)
                or (event.event_metadata or {}).get('extra_metrics',{}) != {key:value for key,value in evidence['usage'].items() if key in EXTRA_METRICS and _nonnegative(value) is not None}
                or (event.event_metadata or {}).get('covered_dispatch_ids',[]) != evidence['coveredDispatchIDs']
                or (event.event_metadata or {}).get('metric_coverage',{}) != evidence['metricCoverage']
                or (event.event_metadata or {}).get('loss_reasons',[]) != evidence['lossReasons']
                or (event.event_metadata or {}).get('identity_coverage') != evidence['identityCoverage']
                or event.status != ('complete' if outcome == 'completed' else outcome)
                or (event.event_metadata or {}).get('normalization_profile') != evidence['normalizationProfile']):
                db.rollback()
                return 'unavailable'
            db.commit()
            return 'recorded'
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def fence_actor_logging(self, identity: Mapping[str, Any]) -> None:
        exact = {key:identity[key] for key in ('sessionID','rootOperationID','actorID','executionRevision')}
        self._capture.fence_actor_execution(self._owner,self._holder_id,exact)

    def _admit_actor_logging(self, request, identity, route, resolved, owner_snapshot=None):
        """Recheck the exact host binding while the bridge holds its finish lock."""
        from core.provider_models import ProviderOperationBinding, ProviderCredentialLease, ProviderShareGrant
        from sqlalchemy import and_
        from core.database import utcnow_naive
        from src.openclank.provider_store import _LOGGING_POLICY_SNAPSHOT_KEY, _validate_logging_policy_snapshot
        exact = {key:identity[key] for key in ('sessionID','rootOperationID','actorID','executionRevision')}
        with self._generation_lock:
            if self._generation_closed.is_set():
                raise ManagedProviderCallbackError('managed provider generation is closed')
            db = self._session_factory()
            try:
                # Capture the grant revision in the existing binding read; an absent or
                # changed grant disables optional attribution without affecting admission.
                pair = db.query(ProviderOperationBinding, ProviderShareGrant.revision).outerjoin(
                    ProviderShareGrant, and_(ProviderShareGrant.id == ProviderOperationBinding.grant_id,
                        ProviderShareGrant.owner == ProviderOperationBinding.credential_owner,
                        ProviderShareGrant.recipient == self._owner,
                        ProviderShareGrant.connection_id == ProviderOperationBinding.connection_id,
                        ProviderShareGrant.state == 'active')).filter(
                    ProviderOperationBinding.owner == self._owner, ProviderOperationBinding.id == request['bindingID'],
                    ProviderOperationBinding.root_operation_id == exact['rootOperationID'],
                    ProviderOperationBinding.engine_session_id == exact['sessionID'],
                    ProviderOperationBinding.actor_id == exact['actorID'],
                    ProviderOperationBinding.execution_revision == exact['executionRevision']).first()
                binding, grant_revision = pair if pair else (None, None)
                if (binding is None or binding.state not in ('uncommitted','committed')
                    or binding.connection_id != route['connectionID'] or binding.model_id != route['modelID']
                    or binding.model_route_id != route['modelRouteID'] or binding.provider_id != route['providerID']
                    or binding.billing_lane != route['billingLane'] or binding.selected_account_id != route['selectedAccountID']
                    or binding.grant_id != route['grantID'] or binding.revision != route['bindingRevision']
                    or binding.credential_revision != route['credentialRevision']):
                    raise ManagedProviderCallbackError('actor logging binding changed')
                expected_private = ({**resolved['authority'],'holderID':self._holder_id} if resolved.get('authority') else None)
                snapshot = binding.policy_snapshot if isinstance(binding.policy_snapshot,Mapping) else {}
                if snapshot.get('private_authority') != expected_private:
                    raise ManagedProviderCallbackError('actor logging privacy authority changed')
                policy = _validate_logging_policy_snapshot(snapshot.get(_LOGGING_POLICY_SNAPSHOT_KEY))
                if policy is None or policy['private'] != bool(expected_private):
                    raise ManagedProviderCallbackError('actor logging policy unavailable')
                if binding.selected_account_id:
                    lease = db.query(ProviderCredentialLease).filter_by(owner=self._owner,holder_id=self._holder_id,
                        root_operation_id=exact['rootOperationID'],binding_id=binding.id,account_id=binding.selected_account_id,
                        connection_id=binding.connection_id,model_id=binding.model_id,grant_id=binding.grant_id,
                        credential_revision=binding.credential_revision).filter(
                        ProviderCredentialLease.expires_at > utcnow_naive()).first()
                    if lease is None:
                        raise ManagedProviderCallbackError('actor logging credential lease expired')
                is_root = exact['actorID'] == 'main' and exact['sessionID'] == resolved.get('rootSessionID')
                with self._logging_context_lock:
                    root = dict(self._logging_contexts.get(exact['rootOperationID']) or {}) if is_root else {}
                operation_id = root.get('operation_id') or ('actor-binding:' + binding.id)
                context = {'owner':self._owner,'holder_id':self._holder_id,'root_operation_id':exact['rootOperationID'],
                    'operation_id':operation_id,'session_id':resolved.get('stableChatID'),
                    'workspace_id':resolved.get('authorityWorkspaceID'),'operation_type':'chat',
                    'actor_id':exact['actorID'],'actor_kind':'foreground' if is_root else 'subagent',
                    'route_id':binding.model_route_id,'incognito':policy['private'],
                    'actor_execution':exact,'logging_policy_snapshot':policy, 'mapping_revision':resolved.get('mappingRevision')}
                if (isinstance(owner_snapshot,dict) and owner_snapshot.get('identity') == exact
                    and owner_snapshot.get('chat') == context['session_id'] and owner_snapshot.get('holder') == self._holder_id):
                    context['host_account_id'] = owner_snapshot.get('account')
                from src.openclank.logging_capture_store import ActorCaptureFenced
                try:
                    return self._capture.admit(owner=self._owner,holder_id=self._holder_id,context=context,binding=binding,
                        original_grant_revision=grant_revision)
                except ActorCaptureFenced:
                    self._capture.policy.release_pin(self._owner,'actor-binding:' + binding.id,holder_id=self._holder_id)
                    raise
            finally:
                db.close()

    def _logging_callback(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method.endswith("/events"):
            return self._capture.events(owner=self._owner, holder_id=self._holder_id, payload=params)
        from core.provider_models import ProviderOperationBinding, ProviderCredentialLease
        from core.database import utcnow_naive
        root = str(params["rootOperationID"])
        with self._logging_context_lock:
            operation_id = params.get("operationID")
            context = dict((self._logging_operations.get(operation_id) if operation_id else self._logging_contexts.get(root)) or {})
            if not operation_id:
                context["operation_id"] = None
        if not context.get("root_operation_id") or context["root_operation_id"] != root:
            raise ManagedProviderCallbackError("logging operation is inactive")
        db = self._session_factory()
        try:
            binding = db.query(ProviderOperationBinding).filter_by(owner=self._owner, id=params["bindingID"], root_operation_id=root).first()
            if binding is None:
                raise ManagedProviderCallbackError("logging binding is foreign")
            if operation_id:
                from core.operation_models import OperationJournal
                journal = db.query(OperationJournal).filter_by(owner=self._owner, id=operation_id, root_operation_id=root).first()
                if journal is None:
                    raise ManagedProviderCallbackError("logging journal is foreign")
            allowed = context.get("allowed_routes") or []
            if allowed and not any(r.get("connectionID") == binding.connection_id and r.get("modelID") == binding.model_id
                                   and r.get("billingLane") == binding.billing_lane for r in allowed):
                raise ManagedProviderCallbackError("logging route escaped operation")
            if context.get("connection_id") and context["connection_id"] != binding.connection_id:
                raise ManagedProviderCallbackError("logging connection escaped operation")
            if binding.selected_account_id:
                lease = db.query(ProviderCredentialLease).filter_by(owner=self._owner, holder_id=self._holder_id,
                    root_operation_id=root, binding_id=binding.id, account_id=binding.selected_account_id).filter(
                    ProviderCredentialLease.expires_at > utcnow_naive()).first()
                if lease is None:
                    raise ManagedProviderCallbackError("logging credential lease is expired")
            if allowed:
                selected = next(r for r in allowed if r.get("connectionID") == binding.connection_id and r.get("modelID") == binding.model_id)
                context["route_id"] = selected.get("modelRouteID")
            with self._logging_context_lock:
                if root not in self._logging_contexts:
                    raise ManagedProviderCallbackError("logging operation ended during admission")
                return self._capture.admit(owner=self._owner, holder_id=self._holder_id, context=context, binding=binding)
        finally:
            db.close()

    async def _validated_fresh_main(self, request):
        """Use the existing actor/bootstrap validation for an unregistered private path."""
        trusted_private_authority = None
        trusted_options = None
        context = await self._validate_actor_execution(
        {
            "sessionID": request["sessionID"],
            "rootOperationID": request["rootOperationID"],
            "actorID": request["actorID"],
            "executionRevision": request["executionRevision"],
        },
        model_id=request["modelID"],
        model_route_id=request["modelRouteID"],
        provider_id=request["providerID"],
        connection_id=request["connectionID"],
        billing_lane=request["billingLane"],
        grant_id=request.get("grantID"),
        )
        bootstrap = request["bootstrap"]
        option_snapshot = request.get("modelRequestOptions")
        if option_snapshot != bootstrap.get("modelRequestOptions"):
            raise ManagedProviderCallbackError("actor model options escaped their preflight")
        if option_snapshot is not None:
            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection, ProviderRequestOptionError
            current_route = await self.resolve_route_context(f"{request['connectionID']}/{request['modelID']}", grant_id=request.get("grantID"))
            if option_snapshot.get("routeID") != current_route.model_route_id or option_snapshot.get("model") != {"providerID":current_route.connection_id, "modelID":current_route.model_id}:
                raise ManagedProviderCallbackError("actor model option route changed; refresh required")
            try:
                validate_request_option_selection(route_id=current_route.option_route_id, catalog_revision=current_route.catalog_revision,
                    descriptors=project_request_options(current_route.option_capabilities or {}, catalog_revision=current_route.catalog_revision, source="engine-catalog"),
                    selection={"route_id":option_snapshot.get("optionRouteID"), "catalog_revision":option_snapshot.get("catalogRevision"), "options":option_snapshot.get("options")})
            except ProviderRequestOptionError as exc:
                raise ManagedProviderCallbackError("actor model options changed; refresh required") from exc
        if request["actorID"] == "main":
            main_candidate = context.get("mainOptionCandidate")
            if main_candidate is not None:
                if (option_snapshot is None or option_snapshot.get("routeID") != main_candidate.get("provider_model_route_id")
                    or option_snapshot.get("model", {}).get("modelID") != main_candidate.get("provider_model_id")
                    or option_snapshot.get("optionRouteID") != main_candidate.get("route_id")
                    or option_snapshot.get("catalogRevision") != main_candidate.get("catalog_revision")
                    or option_snapshot.get("options") != main_candidate.get("options")):
                    raise ManagedProviderCallbackError("main model options escaped their admitted root")
            elif option_snapshot is not None:
                raise ManagedProviderCallbackError("main model option admission is unavailable")
            if not context.get("authority"):
                trusted_options = option_snapshot
        private_authority = context.get("authority")
        if (request["executionRevision"] != bootstrap["expectedExecutionRevision"] + 1
            or bootstrap["modelRouteID"] != request["modelRouteID"]
            or private_authority != bootstrap.get("authority")
            or (not private_authority and (isinstance(context.get("mapRevision"), bool)
                or not isinstance(context.get("mapRevision"), int)
                or context["mapRevision"] < bootstrap["mapRevision"]))
            or any(context.get(key) != bootstrap.get(key) for key in (
                "stableChatID", "owner", "rootSessionID", "sessionID", "actorID",
                "rootOperationID", "mappingRevision", "workspaceRevision"
            ))
            or context.get("handoffID") != bootstrap.get("handoffID")):
            raise ManagedProviderCallbackError("managed actor bootstrap authority changed")
        if private_authority:
            trusted_private_authority = {**private_authority, "holderID": self._holder_id}
        return context, trusted_private_authority, trusted_options

    async def _reserve_fresh_main(self, params):
        """Private F4c-A only; never registered as a managed callback."""
        request = validate_managed_method_request('_openclank/provider-store/v1/account/bind', params)
        if request['scopeKind'] != 'actor_execution' or request['actorID'] != 'main' or request.get('grantID'):
            raise ManagedProviderCallbackError('fresh main reservation unavailable')
        bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
        if bridge is None or bridge._managed_provider_context.__self__ is not self:
            raise ManagedProviderCallbackError('fresh main root unavailable')
        if not bridge._provider_root_sync_guard.acquire(blocking=False):
            raise ManagedProviderCallbackError('fresh main root unavailable')
        try:
            root = bridge._turns.get(request['sessionID'])
            if root is None or root.provider_root_closing:
                raise ManagedProviderCallbackError('fresh main root unavailable')
            attachment = root.foreground_attachment
            from src.openclank.provider_human_policy import CapturedProviderPolicy
            selection = attachment.get('provider_policy_selection') if type(attachment) is dict else None
            if type(selection) is not CapturedProviderPolicy:
                raise ManagedProviderCallbackError('fresh main policy unavailable')
            selection.assert_current()
            if root.provider_root_nonce is None:
                root.provider_root_nonce = secrets.token_hex(32)
        finally:
            bridge._provider_root_sync_guard.release()
        attachment_keys = ('lease', 'worker', 'process', 'barrier', 'admission')
        attachment_objects = tuple(attachment.get(key) for key in attachment_keys)
        attachment_values = tuple(attachment.get(key) for key in ('account','root_binding','generation','owner_epoch'))
        original_account = root.owner_account_id
        original_nonce = root.provider_root_nonce
        pool = getattr(attachment.get('lease'), '_pool', None)
        try: owner_state = pool._states.get(pool._key(self._owner))
        except Exception: raise ManagedProviderCallbackError('fresh main root unavailable') from None
        if owner_state is None: raise ManagedProviderCallbackError('fresh main root unavailable')
        options = json.dumps(root.main_option_candidate, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if (selection.owner != self._owner or selection.account_id != original_account
                or selection.route_id != request['modelRouteID'] or selection.connection_id != request['connectionID']
                or selection.model_id != request['modelID'] or selection.candidate_json != options):
            raise ManagedProviderCallbackError('fresh main policy selection changed')
        holder, callback = root.provider_root_holder, root.provider_root_callback
        context, private_authority, trusted_options = await self._validated_fresh_main(request)
        if (private_authority is not None or context.get('authority') is not None
                or context.get('rootSessionID') != request['sessionID']
                or context.get('effectiveModel') != {'providerID':request['connectionID'], 'modelID':request['modelID']}):
            raise ManagedProviderCallbackError('fresh main authority unavailable')
        request_json = json.dumps(request, sort_keys=True, separators=(',', ':'), allow_nan=False)
        context_json = json.dumps(context, sort_keys=True, separators=(',', ':'), allow_nan=False)
        def transact(admission):
            admission.assert_current()
            if (admission._root is not root or root.foreground_attachment is not attachment
                    or any(attachment.get(key) is not value for key, value in zip(attachment_keys, attachment_objects))
                    or tuple(attachment.get(key) for key in ('account','root_binding','generation','owner_epoch')) != attachment_values
                    or getattr(attachment.get('lease'), '_pool', None) is not pool
                    or pool._states.get(pool._key(self._owner)) is not owner_state
                    or root.owner_account_id != original_account or admission.account_id != original_account
                    or root.provider_root_nonce != original_nonce
                    or admission._callback is not callback or callback is not self
                    or admission.holder_id != holder or admission.option_context_json != options
                    or context.get('stableChatID') != admission.chat_id
                    or context.get('holderID') != holder):
                raise ManagedProviderCallbackError('fresh main root changed')
            if selection.chat_id != admission.chat_id:
                raise ManagedProviderCallbackError('fresh main chat changed')
            proof = _FreshMainBinding(admission, self, root, request_json, context_json, selection, _FRESH_MAIN_SEAL)
            result = self._store.managed_account_bind(owner=self._owner, params=request,
                trusted_options=trusted_options, _fresh=proof)
            db = self._session_factory()
            try:
                from src.openclank.provider_cross_model import profile_ready
                if profile_ready(db.connection()):
                    raw = db.connection().exec_driver_sql('SELECT cross_model_coordinator FROM provider_operation_bindings WHERE id=? AND owner=?',
                        (result['bindingID'], self._owner)).scalar_one()
                    if raw is not None:
                        value = json.loads(raw)
                        if (type(value) is not dict or value.get('state') != 'provisional' or value.get('revision') != 1
                                or value.get('original', {}).get('root_nonce') != root.provider_root_nonce):
                            raise ManagedProviderCallbackError('fresh main reservation unavailable')
                        result['pendingCrossModel'] = {'version':1, 'coordinatorRevision':1}
                admission.assert_current()
                return result
            finally:
                db.close()
        return await bridge.admit_managed_provider_root_transaction(request['sessionID'], request['rootOperationID'], transact)

    def _has_captured_main_policy(self, request):
        """Use the original direct preflight intent; never infer Off on retirement."""
        if request.get('scopeKind') != 'actor_execution' or request.get('actorID') != 'main': return 'not_applicable'
        bridge=getattr(self._actor_execution_context_resolver,'__self__',None)
        if bridge is None: return 'not_applicable'
        if not bridge._provider_root_sync_guard.acquire(blocking=False):
            raise ManagedProviderCallbackError('provider root lifecycle unavailable')
        try:
            matches=[record for record in bridge._prepared_managed_handoffs.values()
                if record.get('directMainPolicyIntent') is not None and record.get('owner')==self._owner
                and record.get('sessionID')==request.get('sessionID') and record.get('rootOperationID')==request.get('rootOperationID')
                and record.get('actorID')=='main' and record.get('expectedExecutionRevision')+1==request.get('executionRevision')]
            if len(matches)>1: raise ManagedProviderCallbackError('provider policy preflight ambiguous')
            root=bridge._turns.get(request.get('sessionID'))
            attachment=getattr(root,'foreground_attachment',None)
            if not matches:
                if root is not None:
                    if (getattr(root,'root_operation_id',None)!=request.get('rootOperationID')
                            or getattr(root,'provider_root_closing',True)
                            or (type(attachment) is dict and attachment.get('provider_policy_intent') is not None)):
                        raise ManagedProviderCallbackError('provider policy preflight unavailable')
                    return 'not_applicable'  # actual live, non-applicable root
                background=[r for r in bridge._prepared_managed_handoffs.values()
                    if r.get('directMainPolicyIntent') is None and r.get('state')=='prepared'
                    and r.get('owner')==self._owner and r.get('sessionID')==request.get('sessionID')
                    and r.get('rootOperationID')==request.get('rootOperationID') and r.get('actorID')=='main'
                    and r.get('expectedExecutionRevision')+1==request.get('executionRevision')]
                if len(background)!=1: raise ManagedProviderCallbackError('provider original root unavailable')
                return 'not_applicable'  # exact trusted background handoff
            record=matches[0];intent=record['directMainPolicyIntent'];bootstrap=request.get('bootstrap')
            from src.openclank.provider_human_policy import CapturedProviderPolicyIntent
            if (not isinstance(bootstrap,dict) or any(bootstrap.get(field)!=record.get(field) for field in
                    ('owner','stableChatID','sessionID','rootSessionID','rootOperationID','actorID',
                     'expectedExecutionRevision','mapRevision','mappingRevision','workspaceRevision'))):
                raise ManagedProviderCallbackError('provider policy preflight identity changed')
            if (type(intent) is not CapturedProviderPolicyIntent or record.get('state')!='prepared'
                    or root is not record.get('directMainRoot') or getattr(root,'provider_root_closing',True)
                    or getattr(root,'root_operation_id',None)!=request.get('rootOperationID')
                    or attachment is not record.get('directMainAttachment')
                    or record.get('directMainHolder')!=self._holder_id
                    or getattr(bridge,'_managed_provider_generation',None)!=self._holder_id):
                raise ManagedProviderCallbackError('provider policy original root unavailable')
            intent.assert_current()
            if not bridge._direct_foreground_current(attachment,owner=self._owner,host_session=record['stableChatID'],
                    native_session=request['sessionID'],root=request['rootOperationID'],holder=self._holder_id):
                raise ManagedProviderCallbackError('provider policy original root changed')
            return intent.state
        finally:
            bridge._provider_root_sync_guard.release()

    async def _bind_original_off_main(self, request):
        """Bind the original primary-only decision while its exact root is held."""
        bridge=getattr(self._actor_execution_context_resolver,'__self__',None)
        if bridge is None or request.get('grantID'):
            raise ManagedProviderCallbackError('original Off root unavailable')
        context,private_authority,trusted_options=await self._validated_fresh_main(request)
        if private_authority is not None or context.get('authority') is not None:
            raise ManagedProviderCallbackError('original Off authority unavailable')
        def transact(admission):
            admission.assert_current()
            if self._has_captured_main_policy(request)!='off':
                raise ManagedProviderCallbackError('original Off intent changed')
            result=self._dispatch_provider_generation('_openclank/provider-store/v1/account/bind',request,None,None,trusted_options)
            admission.assert_current()
            return result
        return await bridge.admit_managed_provider_root_transaction(request['sessionID'],request['rootOperationID'],transact)

    async def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, execute off-loop, and validate one callback round trip."""

        if method not in MANAGED_CALLBACK_METHODS:
            raise ManagedProviderCallbackError("unsupported managed callback")
        try:
            if method == '_openclank/provider-store/v1/account/attempt' and 'successContinuity' in params:
                candidate=params['successContinuity']
                try:
                    valid=(type(candidate) is dict and type(candidate.get('version')) is int
                        and ((candidate['version']==1 and set(candidate)=={'version','admissionID','credentialRevision'})
                             or (candidate['version']==2 and set(candidate)=={'version','admissionID','credentialRevision','previousCredentialRevision'}
                                 and type(candidate['previousCredentialRevision']) is int and candidate['previousCredentialRevision']>=1
                                 and candidate['credentialRevision']==candidate['previousCredentialRevision']+1))
                        and type(candidate.get('admissionID')) is str and 0<len(candidate['admissionID'])<=256
                        and type(candidate.get('credentialRevision')) is int and candidate['credentialRevision']>=1)
                    from src.openclank.provider_store import _validated_dispatch_observation
                    valid=valid and params.get('outcome') in {'success','invalid_output'} and _validated_dispatch_observation(params.get('dispatchObservation'),params['outcome'],None) is not None
                except Exception: valid=False
                if not valid: params={key:value for key,value in params.items() if key not in {'successContinuity','dispatchObservation'}}
            if method == '_openclank/provider-store/v1/account/attempt' and 'refreshContinuity' in params:
                candidate = params['refreshContinuity']
                try:
                    valid = (type(candidate) is dict and set(candidate) == {'version', 'admissionID', 'previousCredentialRevision', 'credentialRevision'}
                        and type(candidate['version']) is int and candidate['version'] == 1
                        and type(candidate['admissionID']) is str and 0 < len(candidate['admissionID']) <= 256
                        and type(candidate['previousCredentialRevision']) is int and candidate['previousCredentialRevision'] >= 1
                        and type(candidate['credentialRevision']) is int and candidate['credentialRevision'] == candidate['previousCredentialRevision'] + 1)
                except Exception:
                    valid = False
                if not valid:
                    params = {key: value for key, value in params.items() if key not in {'refreshContinuity', 'dispatchObservation'}}
            request = validate_managed_method_request(method, params)
            if method == '_openclank/logging/v1/admit':
                identity, route = await asyncio.to_thread(self._binding_execution_identity,request['bindingID'])
                if identity['scopeKind'] == 'actor_execution':
                    if identity['rootOperationID'] != request['rootOperationID']:
                        raise ManagedProviderCallbackError('actor logging root changed')
                    resolved = await self._validate_actor_execution(identity,model_id=route['modelID'],
                        model_route_id=route['modelRouteID'],provider_id=route['providerID'],
                        connection_id=route['connectionID'],billing_lane=route['billingLane'],grant_id=route['grantID'])
                    bridge = getattr(self._actor_execution_context_resolver,'__self__',None)
                    admit = getattr(bridge,'admit_managed_actor_logging',None)
                    if not callable(admit):
                        raise ManagedProviderCallbackError('actor logging lifecycle unavailable')
                    result = await admit(identity,lambda snapshot:self._admit_actor_logging(request,identity,route,resolved,snapshot))
                else:
                    result = await asyncio.to_thread(self._logging_callback,method,request)
            elif method in LOGGING_METHODS:
                result = await asyncio.to_thread(self._logging_callback, method, request)
            elif method == "_openclank/provider-store/v1/actor-route/preflight":
                result = await self._actor_route_preflight(request)
            elif method == '_openclank/provider-store/v1/credential/lease' and 'crossModelAlternative' in request:
                alternative = request['crossModelAlternative']
                bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
                if bridge is None or not callable(getattr(bridge, 'admit_managed_provider_transaction', None)):
                    raise ManagedProviderCallbackError('alternative credential authority unavailable')
                grant, attempt, selection, route = await bridge.admit_managed_provider_transaction(alternative['turn'], lambda admission:
                    self._store.issue_explicit_cross_model_alternative(admission, self, request['bindingID'],
                        request['expectedBindingRevision'], alternative['expectedCoordinatorRevision'], alternative['terminalAttemptID'],
                        expected_credential_revision=request['expectedCredentialRevision']))
                result = {'version':1, 'binding':selection, 'route':route, 'lease':grant.to_wire(), 'attempt':attempt}
            elif method in ('_openclank/provider-store/v1/account/attempt','_openclank/provider-store/v1/account/commit') and (
                    'crossModelDispatch' in request or 'crossModelAlternativeCommit' in request):
                dispatch = method.endswith('/account/attempt')
                control = request['crossModelDispatch' if dispatch else 'crossModelAlternativeCommit']
                if request['bindingID'] != control['primaryBindingID'] or request['expectedRevision'] != control['primaryBindingRevision']:
                    raise ManagedProviderCallbackError('alternative control identity changed')
                bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
                if bridge is None or not callable(getattr(bridge, 'admit_managed_provider_transaction', None)):
                    raise ManagedProviderCallbackError('alternative control authority unavailable')
                operation = self._store.check_explicit_cross_model_dispatch if dispatch else self._store.commit_explicit_cross_model_alternative
                result = await bridge.admit_managed_provider_transaction(control['turn'], lambda admission:
                    operation(admission, self, control['primaryBindingID'], control['primaryBindingRevision'],
                        control['expectedCoordinatorRevision'], control['alternativeBindingID'],
                        control['alternativeBindingRevision'], control['attemptID']))
            elif method == '_openclank/provider-store/v1/credential/lease' and 'crossModelAdmission' in request:
                qualification = request['crossModelAdmission']
                bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
                if bridge is None or not callable(getattr(bridge, 'admit_managed_provider_transaction', None)):
                    raise ManagedProviderCallbackError('qualified credential authority unavailable')
                turn = qualification['turn']
                grant, attempt = await bridge.admit_managed_provider_transaction(turn, lambda admission:
                    self._store.issue_first_cross_model_credential(admission, self, request['bindingID'],
                        request['expectedBindingRevision'], request['expectedCredentialRevision'],
                        qualification['expectedCoordinatorRevision']))
                result = grant.to_wire()
                result['crossModelAttempt'] = {'version':1, 'attemptID':attempt['attemptNonce'],
                    'attemptGeneration':attempt['attemptGeneration'], 'coordinatorRevision':attempt['coordinatorRevision']}
            elif method == '_openclank/provider-store/v1/account/attempt' and 'crossModelTerminal' in request:
                terminal = request['crossModelTerminal']
                bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
                if bridge is None or not callable(getattr(bridge, 'admit_managed_provider_transaction', None)):
                    raise ManagedProviderCallbackError('qualified terminal authority unavailable')
                result = await bridge.admit_managed_provider_transaction(terminal['turn'], lambda admission:
                    self._store.control_cross_model_attempt(admission, self, request['bindingID'], request['expectedRevision'],
                        terminal['attemptID'], terminal['attemptGeneration'], terminal['expectedCoordinatorRevision'],
                        terminal_profile=terminal['profile']))
            elif method in ('_openclank/provider-store/v1/account/commit', '_openclank/provider-store/v1/account/attempt') and 'crossModelControl' in request:
                control = request['crossModelControl']
                bridge = getattr(self._actor_execution_context_resolver, '__self__', None)
                if bridge is None or not callable(getattr(bridge, 'admit_managed_provider_transaction', None)):
                    raise ManagedProviderCallbackError('qualified control authority unavailable')
                is_attempt = method.endswith('/account/attempt')
                if is_attempt and any(key in request for key in ('evidence','dispatchObservation','refreshContinuity','successContinuity','retryAfterMs','modelEligible')):
                    raise ManagedProviderCallbackError('qualified health accounting unavailable')
                result = await bridge.admit_managed_provider_transaction(control['turn'], lambda admission:
                    self._store.control_cross_model_attempt(admission, self, request['bindingID'], request['expectedRevision'],
                        control['attemptID'], control['attemptGeneration'], control['expectedCoordinatorRevision'],
                        outcome=request['outcome'] if is_attempt else None, account_id=request.get('accountID')))
            elif method == '_openclank/provider-store/v1/account/bind' and (main_intent := self._has_captured_main_policy(request)) != 'not_applicable':
                # Selected and Off both retain the exact original root.  A
                # failed qualification never falls back to generic binding.
                result = await (self._reserve_fresh_main(request) if main_intent=='selected' else self._bind_original_off_main(request))
            elif method in PROVIDER_CALLBACK_METHODS:
                trusted_private_authority = None
                trusted_options = None
                auxiliary = None
                if method == "_openclank/provider-store/v1/account/bind":
                    if request["scopeKind"] == "auxiliary_operation":
                        auxiliary = (request, {**request, "credentialOwner": self._owner})
                    else:
                        context = await self._validate_actor_execution(
                        {
                            "sessionID": request["sessionID"],
                            "rootOperationID": request["rootOperationID"],
                            "actorID": request["actorID"],
                            "executionRevision": request["executionRevision"],
                        },
                        model_id=request["modelID"],
                        model_route_id=request["modelRouteID"],
                        provider_id=request["providerID"],
                        connection_id=request["connectionID"],
                        billing_lane=request["billingLane"],
                        grant_id=request.get("grantID"),
                        )
                        bootstrap = request["bootstrap"]
                        option_snapshot = request.get("modelRequestOptions")
                        if option_snapshot != bootstrap.get("modelRequestOptions"):
                            raise ManagedProviderCallbackError("actor model options escaped their preflight")
                        if option_snapshot is not None:
                            from src.openclank.provider_request_options import project_request_options, validate_request_option_selection, ProviderRequestOptionError
                            current_route = await self.resolve_route_context(f"{request['connectionID']}/{request['modelID']}", grant_id=request.get("grantID"))
                            if option_snapshot.get("routeID") != current_route.model_route_id or option_snapshot.get("model") != {"providerID":current_route.connection_id, "modelID":current_route.model_id}:
                                raise ManagedProviderCallbackError("actor model option route changed; refresh required")
                            try:
                                validate_request_option_selection(route_id=current_route.option_route_id, catalog_revision=current_route.catalog_revision,
                                    descriptors=project_request_options(current_route.option_capabilities or {}, catalog_revision=current_route.catalog_revision, source="engine-catalog"),
                                    selection={"route_id":option_snapshot.get("optionRouteID"), "catalog_revision":option_snapshot.get("catalogRevision"), "options":option_snapshot.get("options")})
                            except ProviderRequestOptionError as exc:
                                raise ManagedProviderCallbackError("actor model options changed; refresh required") from exc
                        if request["actorID"] == "main":
                            main_candidate = context.get("mainOptionCandidate")
                            if main_candidate is not None:
                                if (option_snapshot is None or option_snapshot.get("routeID") != main_candidate.get("provider_model_route_id")
                                    or option_snapshot.get("model", {}).get("modelID") != main_candidate.get("provider_model_id")
                                    or option_snapshot.get("optionRouteID") != main_candidate.get("route_id")
                                    or option_snapshot.get("catalogRevision") != main_candidate.get("catalog_revision")
                                    or option_snapshot.get("options") != main_candidate.get("options")):
                                    raise ManagedProviderCallbackError("main model options escaped their admitted root")
                            elif option_snapshot is not None:
                                raise ManagedProviderCallbackError("main model option admission is unavailable")
                            if not context.get("authority"):
                                trusted_options = option_snapshot
                        private_authority = context.get("authority")
                        if (request["executionRevision"] != bootstrap["expectedExecutionRevision"] + 1
                            or bootstrap["modelRouteID"] != request["modelRouteID"]
                            or private_authority != bootstrap.get("authority")
                            or (not private_authority and (isinstance(context.get("mapRevision"), bool)
                                or not isinstance(context.get("mapRevision"), int)
                                or context["mapRevision"] < bootstrap["mapRevision"]))
                            or any(context.get(key) != bootstrap.get(key) for key in (
                                "stableChatID", "owner", "rootSessionID", "sessionID", "actorID",
                                "rootOperationID", "mappingRevision", "workspaceRevision"
                            ))
                            or context.get("handoffID") != bootstrap.get("handoffID")):
                            raise ManagedProviderCallbackError("managed actor bootstrap authority changed")
                        if private_authority:
                            trusted_private_authority = {**private_authority, "holderID": self._holder_id}
                elif method in {
                    "_openclank/provider-store/v1/refresh/acquire",
                    "_openclank/provider-store/v1/refresh/renew",
                    "_openclank/provider-store/v1/refresh/commit",
                } and request.get("bindingID"):
                    identity, route = await asyncio.to_thread(self._binding_execution_identity, request["bindingID"])
                    if identity["scopeKind"] == "actor_execution":
                        current = await self._validate_actor_execution(identity, model_id=route["modelID"],
                            model_route_id=route["modelRouteID"], provider_id=route["providerID"],
                            connection_id=route["connectionID"], billing_lane=route["billingLane"], grant_id=route["grantID"])
                        if route.get("privateAuthority") != ({**current["authority"], "holderID": self._holder_id} if current.get("authority") else None):
                            raise ManagedProviderCallbackError("provider binding private authority changed")
                    else:
                        auxiliary = (identity, route)
                elif method in {
                    "_openclank/provider-store/v1/account/commit",
                    "_openclank/provider-store/v1/account/attempt",
                    "_openclank/provider-store/v1/credential/lease",
                }:
                    identity, route = await asyncio.to_thread(
                        self._binding_execution_identity, request["bindingID"]
                    )
                    if request.get("rootOperationID") not in (None, identity["rootOperationID"]):
                        raise ManagedProviderCallbackError("provider operation root does not match its binding")
                    if identity["scopeKind"] == "actor_execution":
                        current = await self._validate_actor_execution(
                            identity,
                            model_id=route["modelID"],
                            model_route_id=route["modelRouteID"],
                            provider_id=route["providerID"],
                            connection_id=route["connectionID"],
                            billing_lane=route["billingLane"],
                            grant_id=route["grantID"],
                        )
                        if route.get("privateAuthority") != ({**current["authority"], "holderID": self._holder_id} if current.get("authority") else None):
                            raise ManagedProviderCallbackError("provider binding private authority changed")
                    else:
                        auxiliary = (identity, route)
                result = await asyncio.to_thread(self._dispatch_provider_generation, method, request, trusted_private_authority, auxiliary, trusted_options)
            elif method == "_openclank/operations/v1/journal/cas":
                result = await asyncio.to_thread(self._journal_callback, request)
            elif method == "_openclank/operations/v1/artifact/read":
                result = await asyncio.to_thread(
                    self._artifact_read_callback,
                    request,
                )
            elif method == "_openclank/operations/v1/artifact/write":
                result = await asyncio.to_thread(
                    self._artifact_write_callback,
                    request,
                )
            elif method == "_openclank/operations/v1/executor/invoke":
                result = await self._executor_callback(request)
            else:  # pragma: no cover - guarded by MANAGED_CALLBACK_METHODS
                raise ManagedProviderCallbackError("unsupported managed callback")
            return validate_managed_method_result(method, result)
        except ManagedMethodValidationError as exc:
            raise ManagedProviderCallbackError(str(exc)) from None
        except ManagedProviderCallbackError:
            raise
        except ProviderStoreError as exc:
            # ProviderStore errors are intentionally written without credential
            # values. Preserve their useful classification while keeping the
            # JSON-RPC error body safe for logs and clients.
            raise ManagedProviderCallbackError(
                f"managed provider store rejected {method.rsplit('/', 1)[-1]}: {exc}"
            ) from None
        except (OperationJournalError, ArtifactError, LocalExecutorError) as exc:
            # These stores deliberately emit identifier-only diagnostics. Keep
            # the useful classification while never echoing callback payloads.
            raise ManagedProviderCallbackError(
                f"managed operation rejected {method.rsplit('/', 1)[-1]}: {exc}"
            ) from None
        except (TypeError, ValueError, KeyError):
            raise ManagedProviderCallbackError(
                "managed callback contained invalid fields"
            ) from None
        except Exception:
            # Database drivers and crypto libraries may include bound values in
            # their native exceptions. Never forward those details to ACP.
            raise ManagedProviderCallbackError(
                "managed callback failed"
            ) from None

    def register(self, client: Any) -> None:
        """Register exact owner-bound methods on an :class:`ACPClient`."""

        prepare_managed_method_validation()
        for method in MANAGED_CALLBACK_METHODS:
            async def callback(
                params: dict,
                *,
                _method: str = method,
            ) -> dict[str, Any]:
                return await self.dispatch(_method, params)

            client.register_callback(method, callback)

    async def resolve_route_context(
        self,
        runtime_model: str,
        *,
        grant_id: Optional[str] = None,
    ) -> ManagedProviderRouteContext:
        """Resolve a displayed engine model to one unambiguous durable route.

        This lookup returns only nonsecret identifiers.  Credential access is a
        later, separately validated callback after the account binding commits.
        """

        return await asyncio.to_thread(
            self._resolve_route_context_sync,
            runtime_model,
            grant_id=grant_id,
        )

    def _resolve_route_context_sync(
        self,
        runtime_model: str,
        *,
        grant_id: Optional[str] = None,
    ) -> ManagedProviderRouteContext:
        from routes.prefs_routes import PREFERENCE_LOCK
        with PREFERENCE_LOCK:
            return self._resolve_route_context_locked(runtime_model, grant_id=grant_id)

    def _resolve_route_context_locked(
        self, runtime_model: str, *, grant_id: Optional[str] = None,
    ) -> ManagedProviderRouteContext:
        runtime_model = _required_text(runtime_model, "runtime model")
        runtime_provider, separator, provider_model = runtime_model.partition("/")
        if not separator or not provider_model:
            raise ManagedProviderRouteError(
                "managed model must use a connection/model identity"
            )

        db = self._session_factory()
        try:
            credential_owner = self._owner
            fixed_connection_id: Optional[str] = None
            normalized_grant_id: Optional[str] = None
            grant_revision: Optional[int] = None
            if grant_id:
                normalized_grant_id = _required_text(grant_id, "provider grant")
                grant = (
                    db.query(ProviderShareGrant)
                    .filter(
                        ProviderShareGrant.id == normalized_grant_id,
                        ProviderShareGrant.recipient == self._owner,
                        ProviderShareGrant.state == "active",
                    )
                    .first()
                )
                if grant is None:
                    raise ManagedProviderRouteError(
                        "managed provider grant is not active"
                    )
                credential_owner = grant.owner
                grant_revision = int(grant.revision)
                fixed_connection_id = grant.connection_id

            query = (
                db.query(ProviderModelRoute, ProviderConnection)
                .join(
                    ProviderConnection,
                    ProviderConnection.id == ProviderModelRoute.connection_id,
                )
                .filter(
                    ProviderModelRoute.owner == credential_owner,
                    ProviderConnection.owner == credential_owner,
                    ProviderModelRoute.provider_model_id == provider_model,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                    ProviderConnection.enabled.is_(True),
                    ProviderConnection.deleted_at.is_(None),
                )
            )
            if fixed_connection_id:
                query = query.filter(ProviderConnection.id == fixed_connection_id)
            rows = query.all()

            exact = [row for row in rows if row[1].id == runtime_provider]
            candidates = exact or [
                row for row in rows if row[1].family_id == runtime_provider
            ]
            if len(candidates) != 1:
                raise ManagedProviderRouteError(
                    "managed model does not resolve to one enabled connection"
                )
            route, connection = candidates[0]
            if grant_id and not ModelSelector.parse(grant.model_selector).includes(route.id):
                raise ManagedProviderRouteError(
                    "managed model route is outside the active grant"
                )
            operations = set(route.operations or ())
            if not {"chat.stream", "chat.complete"}.intersection(operations):
                raise ManagedProviderRouteError(
                    "managed model route does not support chat execution"
                )
            method_policy = None
            if connection.family_id == "anthropic" and connection.billing_lane == "subscription":
                from src.openclank.provider_method_policy import require_enabled, ProviderMethodPolicyError
                try:
                    revision = require_enabled(credential_owner)
                except ProviderMethodPolicyError as exc:
                    raise ManagedProviderRouteError("Claude subscription Beta is unavailable") from exc
                method_policy = ProviderMethodPolicyProof(credential_owner, revision)
            from src.openclank.chat_routing import _shared_request_option_route_id
            option_route_id = _shared_request_option_route_id(self._owner, normalized_grant_id, route.id) if normalized_grant_id else str(route.id)
            return ManagedProviderRouteContext(
                catalog_revision=max(1, int(route.catalog_revision or 1)),
                option_route_id=option_route_id,
                option_capabilities=dict(route.capabilities or {}),
                adapter_id=connection.adapter_id,
                connection_kind=connection.kind,
                connection_id=connection.id,
                provider_id=connection.family_id,
                billing_lane=connection.billing_lane,
                model_id=route.provider_model_id,
                model_route_id=route.id,
                grant_id=normalized_grant_id,
                grant_revision=grant_revision,
                method_policy=method_policy,
            )
        finally:
            db.close()


__all__ = [
    "MANAGED_CALLBACK_METHODS",
    "OPERATION_CALLBACK_METHODS",
    "PROVIDER_CALLBACK_METHODS",
    "ManagedProviderCallbackError",
    "ManagedProviderCallbacks",
    "ManagedProviderRouteContext",
    "ManagedProviderRouteError",
]

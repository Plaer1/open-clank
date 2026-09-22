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
from dataclasses import dataclass
from datetime import timezone
import hashlib
from pathlib import Path
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
)
from src.openclank.generated.managed_provider_contract import (
    OPERATION_METHODS,
    PROVIDER_STORE_METHODS,
)
from src.openclank.artifacts import ArtifactError, ArtifactStore
from src.openclank.local_executor import LocalExecutorBroker, LocalExecutorError
from src.openclank.operation_journal import (
    OperationJournalError,
    OperationJournalStore,
)
from src.openclank.provider_store import ProviderStore, ProviderStoreError


_GENERATED_CALLBACK_METHODS = tuple(
    method
    for method in (*PROVIDER_STORE_METHODS, *OPERATION_METHODS)
    if METHOD_DIRECTIONS[method] == "engine_to_host"
)
if frozenset(_GENERATED_CALLBACK_METHODS) != HOST_CALLBACK_METHODS:
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


@dataclass(frozen=True)
class ManagedProviderRouteContext:
    connection_id: str
    provider_id: str
    billing_lane: str
    model_id: str
    model_route_id: str
    grant_id: Optional[str] = None

    def to_wire(self, *, root_operation_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {
            "rootOperationID": root_operation_id,
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "modelID": self.model_id,
            "modelRouteID": self.model_route_id,
        }
        if self.grant_id:
            result["grantID"] = self.grant_id
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

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def holder_id(self) -> str:
        return self._holder_id

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

    async def dispatch(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Validate, execute off-loop, and validate one callback round trip."""

        if method not in MANAGED_CALLBACK_METHODS:
            raise ManagedProviderCallbackError("unsupported managed callback")
        try:
            request = validate_managed_method_request(method, params)
            if method in PROVIDER_CALLBACK_METHODS:
                result = await asyncio.to_thread(
                    self._store.handle_managed_method,
                    owner=self._owner,
                    holder_id=self._holder_id,
                    method=method,
                    params=request,
                )
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
            operations = set(route.operations or ())
            if not {"chat.stream", "chat.complete"}.intersection(operations):
                raise ManagedProviderRouteError(
                    "managed model route does not support chat execution"
                )
            return ManagedProviderRouteContext(
                connection_id=connection.id,
                provider_id=connection.family_id,
                billing_lane=connection.billing_lane,
                model_id=route.provider_model_id,
                model_route_id=route.id,
                grant_id=normalized_grant_id,
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

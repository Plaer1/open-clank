"""Typed host boundary for every server-side model operation.

Open Clank authorizes an owner and resolves durable model-route identifiers;
the managed engine owns provider semantics, account selection, credential
leases, retries, and execution.  Binary data crosses the boundary only as
owner-scoped content-addressed artifacts.

This module deliberately contains no provider URL, header, or credential
handling.  Callers that still need any of those concepts have not completed
the provider cutover.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Optional, Sequence
import uuid

from core.database import SessionLocal
from core.provider_models import (
    ProviderConnection,
    ProviderModelRoute,
    ProviderRouteBinding,
    ProviderShareGrant,
)
from src.openclank.artifacts import ArtifactError, ArtifactStore

logger = logging.getLogger(__name__)


MODEL_OPERATIONS = frozenset(
    {
        "chat.complete",
        "web.search",
        "vision.describe",
        "image.generate",
        "image.edit",
        "image.inpaint",
        "image.img2img",
        "image.upscale",
        "image.denoise",
        "image.segment",
        "image.remove_background",
        "image.restore_face",
        "audio.synthesize",
        "audio.transcribe",
        "embeddings.create",
    }
)

PURPOSE_OPERATIONS: Mapping[str, frozenset[str]] = {
    "chat": frozenset({"chat.complete"}),
    "utility": frozenset({"chat.complete"}),
    # Memory photo extraction is still the Memory purpose: the route must be
    # explicitly image-aware, and the resolver must use this same binding.
    "memory": frozenset({"chat.complete", "vision.describe"}),
    "research": frozenset({"chat.complete"}),
    "search": frozenset({"web.search"}),
    "tasks": frozenset({"chat.complete"}),
    "vision": frozenset({"vision.describe"}),
    "images": frozenset(
        {
            "image.generate",
            "image.edit",
            "image.inpaint",
            "image.img2img",
            "image.upscale",
            "image.denoise",
            "image.segment",
            "image.remove_background",
            "image.restore_face",
        }
    ),
    "tts": frozenset({"audio.synthesize"}),
    "stt": frozenset({"audio.transcribe"}),
    "embeddings": frozenset({"embeddings.create"}),
}

# A missing purpose binding means inheritance, not a copied point-in-time
# default.  In particular, Memory follows Utility until the owner selects a
# dedicated Memory model; Utility in turn follows Chat.  Keeping the purpose on
# the request preserves policy/audit identity while resolving the same exact
# model route.
_PURPOSE_BINDING_CHAINS: Mapping[str, tuple[str, ...]] = {
    "utility": ("utility", "chat"),
    "memory": ("memory", "utility", "chat"),
}


def _binding_purposes(purpose: str) -> tuple[str, ...]:
    return _PURPOSE_BINDING_CHAINS.get(purpose, (purpose,))

OPERATION_PURPOSE: Mapping[str, str] = {
    "chat.complete": "chat",
    "web.search": "search",
    "vision.describe": "vision",
    "image.generate": "images",
    "image.edit": "images",
    "image.inpaint": "images",
    "image.img2img": "images",
    "image.upscale": "images",
    "image.denoise": "images",
    "image.segment": "images",
    "image.remove_background": "images",
    "image.restore_face": "images",
    "audio.synthesize": "tts",
    "audio.transcribe": "stt",
    "embeddings.create": "embeddings",
}

_BINARY_OUTPUT_OPERATIONS = frozenset(
    {
        "image.generate",
        "image.edit",
        "image.inpaint",
        "image.img2img",
        "image.upscale",
        "image.denoise",
        "image.segment",
        "image.remove_background",
        "image.restore_face",
        "audio.synthesize",
    }
)
_TEXT_OUTPUT_OPERATIONS = frozenset(
    {"chat.complete", "vision.describe", "audio.transcribe"}
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "credential",
        "credentials",
        "password",
        "secret",
        "token",
        "headers",
        "url",
        "uri",
        "path",
        "command",
        "cmd",
        "environment",
        "env",
    }
)


class ManagedOperationError(RuntimeError):
    """Controlled, credential-free operation-router failure."""


class ManagedOperationUnavailable(ManagedOperationError):
    pass


class ManagedOperationDenied(ManagedOperationError):
    pass


class ManagedOperationProtocolError(ManagedOperationError):
    pass


def _required(value: Any, label: str, *, identifier: bool = False) -> str:
    clean = str(value or "").strip()
    if not clean or "\x00" in clean:
        raise ManagedOperationError(f"{label} is required")
    if identifier and not _IDENTIFIER.fullmatch(clean):
        raise ManagedOperationError(f"{label} is invalid")
    return clean


def _owner(value: Any) -> str:
    return _required(value, "operation owner").lower()


def _safe_json(value: Any, *, label: str, depth: int = 0) -> Any:
    """Copy JSON input while refusing covert provider/filesystem authority."""

    if depth > 16:
        raise ManagedOperationError(f"{label} is too deeply nested")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name.strip().lower() in _SENSITIVE_KEYS:
                raise ManagedOperationDenied(
                    f"{label} contains forbidden field {name!r}"
                )
            result[name] = _safe_json(child, label=label, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [
            _safe_json(child, label=label, depth=depth + 1)
            for child in value
        ]
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and value.startswith("data:"):
            raise ManagedOperationDenied(
                f"{label} must use an artifact instead of inline binary data"
            )
        return value
    raise ManagedOperationError(f"{label} is not JSON-safe")


def _validate_search_input(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the narrow host-owned web.search request shape."""

    query = value.get("query")
    if not isinstance(query, str) or not query.strip() or len(query) > 2000:
        raise ManagedOperationError("web.search query is invalid")
    count = value.get("count", 10)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 50:
        raise ManagedOperationError("web.search count is invalid")
    freshness = value.get("freshness")
    if freshness is not None and freshness not in {"day", "week", "month", "year"}:
        raise ManagedOperationError("web.search freshness is invalid")
    answer = value.get("answer", False)
    if not isinstance(answer, bool):
        raise ManagedOperationError("web.search answer flag is invalid")
    safe = _safe_json(dict(value), label="web.search input")
    allowed = {"query", "count", "freshness", "answer"}
    unexpected = set(safe) - allowed
    if unexpected:
        raise ManagedOperationDenied("web.search input contains unsupported fields")
    return safe


def _validate_search_output(value: Any) -> dict[str, Any]:
    """Validate provider annotations without weakening the general safe JSON guard."""

    if not isinstance(value, Mapping):
        raise ManagedOperationProtocolError("web.search output must be an object")
    results = value.get("results", [])
    if not isinstance(results, list) or len(results) > 50:
        raise ManagedOperationProtocolError("web.search results are invalid")
    clean_results: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, Mapping):
            raise ManagedOperationProtocolError("web.search result row is invalid")
        url = row.get("url")
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise ManagedOperationProtocolError("web.search source URL is invalid")
        clean: dict[str, Any] = {"url": url}
        for key in ("title", "snippet", "publishedAt", "provider"):
            if key in row:
                if not isinstance(row[key], str):
                    raise ManagedOperationProtocolError("web.search source annotation is invalid")
                clean[key] = row[key]
        clean_results.append(clean)
    search_queries = value.get("searchQueries", [])
    if not isinstance(search_queries, list) or any(not isinstance(item, str) for item in search_queries):
        raise ManagedOperationProtocolError("web.search queries are invalid")
    support_links = value.get("supportLinks", [])
    if not isinstance(support_links, list) or any(not isinstance(item, str) or not item.startswith(("https://", "http://")) for item in support_links):
        raise ManagedOperationProtocolError("web.search support links are invalid")
    usage = value.get("webSearchUsage", {})
    if not isinstance(usage, Mapping) or any(
        not isinstance(item, str) or isinstance(number, bool) or not isinstance(number, int) or number < 0
        for item, number in usage.items()
    ):
        raise ManagedOperationProtocolError("web.search usage is invalid")
    answer = value.get("answer")
    if answer is not None and not isinstance(answer, str):
        raise ManagedOperationProtocolError("web.search answer is invalid")
    status = value.get("status", "ungrounded" if not clean_results else "complete")
    if status not in {"complete", "partial", "ungrounded", "empty"}:
        raise ManagedOperationProtocolError("web.search status is invalid")
    return {
        "results": clean_results,
        "status": status,
        **({"answer": answer} if answer is not None else {}),
        **({"searchQueries": search_queries} if search_queries else {}),
        **({"supportLinks": support_links} if support_links else {}),
        **({"webSearchUsage": dict(usage)} if usage else {}),
    }


@dataclass(frozen=True, slots=True)
class OperationArtifact:
    name: str
    artifact_id: str
    content_sha256: str
    size_bytes: int
    media_type: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "artifactID": self.artifact_id,
            "contentSHA256": self.content_sha256,
            "sizeBytes": self.size_bytes,
            "mediaType": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class OperationRoute:
    connection_id: str
    provider_id: str
    billing_lane: str
    model_route_id: str
    model_id: str
    grant_id: Optional[str] = None
    preferred_account_id: Optional[str] = None
    inherited_account_id: Optional[str] = None

    def to_wire(self) -> dict[str, Any]:
        result = {
            "connectionID": self.connection_id,
            "providerID": self.provider_id,
            "billingLane": self.billing_lane,
            "modelRouteID": self.model_route_id,
            "modelID": self.model_id,
        }
        for key, value in (
            ("grantID", self.grant_id),
            ("preferredAccountID", self.preferred_account_id),
            ("inheritedAccountID", self.inherited_account_id),
        ):
            if value:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class ManagedOperationRequest:
    owner: str
    operation: str
    input: Mapping[str, Any]
    purpose: Optional[str] = None
    model_route_id: Optional[str] = None
    grant_id: Optional[str] = None
    preferred_account_id: Optional[str] = None
    inherited_account_id: Optional[str] = None
    root_operation_id: str = field(
        default_factory=lambda: f"root_{uuid.uuid4().hex}"
    )
    idempotency_key: str = field(
        default_factory=lambda: f"idem_{uuid.uuid4().hex}"
    )
    artifacts: Sequence[OperationArtifact] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ManagedOperationResult:
    operation_id: str
    root_operation_id: str
    operation: str
    state: str
    committed: bool
    replayed: bool
    model_route_id: str
    connection_id: str
    billing_lane: str
    output: Mapping[str, Any]
    artifacts: tuple[OperationArtifact, ...]
    commit_reason: Optional[str] = None
    binding_id: Optional[str] = None
    selected_account_id: Optional[str] = None
    usage: Mapping[str, int] = field(default_factory=dict)
    # The native producer's normalization profile is provenance for Stats;
    # it must not be replaced with the host's default when producers already
    # separated cache/reasoning categories.
    normalization_profile: Optional[str] = None
    model_fingerprint: Optional[str] = None
    dimension: Optional[int] = None


EngineExecutor = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class ManagedOperationRouter:
    """Authorize routes, invoke the owner worker, and verify its result."""

    def __init__(
        self,
        *,
        session_factory=SessionLocal,
        artifact_store: Optional[ArtifactStore] = None,
        executor: Optional[EngineExecutor] = None,
    ) -> None:
        self._session_factory = session_factory
        if artifact_store is None:
            from src.constants import DATA_DIR

            artifact_store = ArtifactStore(
                Path(DATA_DIR) / "model-artifacts",
                session_factory=session_factory,
            )
        self.artifacts = artifact_store
        self._executor = executor

    def route_preflight(
        self,
        *,
        owner: str,
        purpose: str,
        operation: str,
        include_provider_family: bool = False,
    ) -> dict[str, Any]:
        """Describe an owner's safe choices for one managed route purpose.

        This is a read-only preflight surface for product flows that need an
        explicit model choice before execution.  It deliberately returns no
        provider URL, account, grant, or credential material.
        """

        normalized_owner = _owner(owner)
        normalized_purpose = _required(purpose, "operation purpose").lower()
        normalized_operation = _required(operation, "model operation")
        allowed = PURPOSE_OPERATIONS.get(normalized_purpose)
        if allowed is None or normalized_operation not in allowed:
            raise ManagedOperationDenied(
                "model operation is not valid for the requested purpose"
            )

        db = self._session_factory()
        try:
            binding_purposes = _binding_purposes(normalized_purpose)
            bindings = (
                db.query(ProviderRouteBinding)
                .filter(
                    ProviderRouteBinding.owner == normalized_owner,
                    ProviderRouteBinding.purpose.in_(binding_purposes),
                )
                .all()
            )
            binding_revision = max(
                (int(row.revision) for row in bindings),
                default=0,
            )
            selected_binding_id = None
            for binding_purpose in binding_purposes:
                candidates = sorted(
                    (
                        row for row in bindings
                        if row.purpose == binding_purpose and row.enabled
                    ),
                    key=lambda row: (int(row.ordinal), str(row.id)),
                )
                if candidates:
                    selected_binding_id = candidates[0].model_route_id
                    break

            rows = (
                db.query(ProviderModelRoute, ProviderConnection)
                .join(
                    ProviderConnection,
                    ProviderConnection.id == ProviderModelRoute.connection_id,
                )
                .filter(
                    ProviderModelRoute.owner == normalized_owner,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                    ProviderConnection.owner == normalized_owner,
                    ProviderConnection.enabled.is_(True),
                    ProviderConnection.deleted_at.is_(None),
                )
                .order_by(
                    ProviderConnection.label,
                    ProviderModelRoute.display_name,
                    ProviderModelRoute.id,
                )
                .all()
            )
            choices = [
                {
                    "model_route_id": route.id,
                    "display_name": route.display_name,
                    "connection_label": connection.label,
                }
                for route, connection in rows
                if normalized_operation in set(route.operations or ())
            ]
            if include_provider_family:
                family_by_route = {route.id: connection.family_id for route, connection in rows}
                choices = [
                    {**choice, "provider_family": family_by_route.get(choice["model_route_id"])}
                    for choice in choices
                ]
            eligible_ids = {
                choice["model_route_id"] for choice in choices
            }
            result = {
                "configured": selected_binding_id in eligible_ids,
                "selected_model_route_id": selected_binding_id,
                "binding_revision": binding_revision,
                "eligible_routes": choices,
            }
            if include_provider_family and selected_binding_id:
                selected = next((choice for choice in choices if choice["model_route_id"] == selected_binding_id), None)
                result["selected_provider_family"] = selected.get("provider_family") if selected else None
            return result
        finally:
            db.close()

    def stage_artifact(
        self,
        *,
        owner: str,
        name: str,
        data: bytes,
        media_type: str,
    ) -> OperationArtifact:
        if not isinstance(data, bytes):
            raise ManagedOperationError("artifact data must be bytes")
        row = self.artifacts.put(
            owner=_owner(owner),
            chunks=(data,),
            media_type=media_type,
        )
        return OperationArtifact(
            name=_required(name, "artifact name", identifier=True),
            artifact_id=row.id,
            content_sha256=row.content_sha256,
            size_bytes=int(row.size_bytes),
            media_type=row.media_type,
        )

    def read_artifact(
        self,
        *,
        owner: str,
        artifact: OperationArtifact,
        acknowledge: bool = False,
    ) -> bytes:
        normalized_owner = _owner(owner)
        row = self.artifacts.get(
            owner=normalized_owner,
            artifact_id=artifact.artifact_id,
        )
        self._verify_artifact_row(row, artifact)
        data = b"".join(
            self.artifacts.read_chunks(
                owner=normalized_owner,
                artifact_id=artifact.artifact_id,
            )
        )
        if acknowledge:
            self.artifacts.acknowledge(
                owner=normalized_owner,
                artifact_id=artifact.artifact_id,
            )
        return data

    @staticmethod
    def _verify_artifact_row(row: Any, artifact: OperationArtifact) -> None:
        if (
            row.content_sha256 != artifact.content_sha256
            or int(row.size_bytes) != artifact.size_bytes
            or row.media_type != artifact.media_type
        ):
            raise ManagedOperationProtocolError(
                "managed engine returned a mismatched artifact descriptor"
            )

    @staticmethod
    def _model_selector_includes(grant: ProviderShareGrant, route_id: str) -> bool:
        selector = grant.model_selector or {}
        mode = selector.get("mode")
        if mode == "all_live_models":
            return True
        return (
            mode == "explicit_models"
            and route_id in (selector.get("model_route_ids") or ())
        )

    def _resolve_routes(self, request: ManagedOperationRequest) -> tuple[OperationRoute, ...]:
        owner = _owner(request.owner)
        operation = _required(request.operation, "operation")
        if operation not in MODEL_OPERATIONS:
            raise ManagedOperationError("unsupported managed model operation")
        purpose = str(request.purpose or OPERATION_PURPOSE.get(operation) or "").strip().lower()
        if purpose not in PURPOSE_OPERATIONS or operation not in PURPOSE_OPERATIONS[purpose]:
            raise ManagedOperationError("operation does not match its route purpose")

        db = self._session_factory()
        try:
            credential_owner = owner
            grant: Optional[ProviderShareGrant] = None
            if request.grant_id:
                grant = (
                    db.query(ProviderShareGrant)
                    .filter(
                        ProviderShareGrant.id == str(request.grant_id),
                        ProviderShareGrant.recipient == owner,
                        ProviderShareGrant.state == "active",
                    )
                    .first()
                )
                if grant is None:
                    raise ManagedOperationDenied(
                        "provider share is not active"
                    )
                credential_owner = grant.owner

            query = (
                db.query(ProviderModelRoute, ProviderConnection)
                .join(
                    ProviderConnection,
                    ProviderConnection.id == ProviderModelRoute.connection_id,
                )
                .filter(
                    ProviderModelRoute.owner == credential_owner,
                    ProviderConnection.owner == credential_owner,
                    ProviderModelRoute.enabled.is_(True),
                    ProviderModelRoute.deleted_at.is_(None),
                    ProviderConnection.enabled.is_(True),
                    ProviderConnection.deleted_at.is_(None),
                )
            )
            if request.model_route_id:
                query = query.filter(
                    ProviderModelRoute.id == str(request.model_route_id)
                )
                ordered_rows = query.all()
            elif grant is not None:
                # Shared execution must always name a stable route.  There is
                # no implicit broadening from the recipient's personal default.
                raise ManagedOperationDenied(
                    "shared operations require an explicit model route"
                )
            else:
                ordered_rows = []
                for binding_purpose in _binding_purposes(purpose):
                    ordered_rows = (
                        query.join(
                            ProviderRouteBinding,
                            ProviderRouteBinding.model_route_id
                            == ProviderModelRoute.id,
                        )
                        .filter(
                            ProviderRouteBinding.owner == owner,
                            ProviderRouteBinding.purpose == binding_purpose,
                            ProviderRouteBinding.enabled.is_(True),
                        )
                        .order_by(
                            ProviderRouteBinding.ordinal,
                            ProviderRouteBinding.id,
                        )
                        .limit(1)
                        .all()
                    )
                    if ordered_rows:
                        break

            routes: list[OperationRoute] = []
            for model_route, connection in ordered_rows:
                if operation not in set(model_route.operations or ()):
                    if request.model_route_id:
                        raise ManagedOperationDenied(
                            "model route does not support the requested operation"
                        )
                    continue
                if grant is not None:
                    if connection.id != grant.connection_id:
                        raise ManagedOperationDenied(
                            "shared model route escaped its fixed connection"
                        )
                    if connection.billing_lane != grant.billing_lane:
                        raise ManagedOperationDenied(
                            "shared model route escaped its fixed billing lane"
                        )
                    if not self._model_selector_includes(grant, model_route.id):
                        raise ManagedOperationDenied(
                            "shared model route is outside the grant selector"
                        )
                routes.append(
                    OperationRoute(
                        connection_id=connection.id,
                        provider_id=connection.family_id,
                        billing_lane=connection.billing_lane,
                        model_route_id=model_route.id,
                        model_id=model_route.provider_model_id,
                        grant_id=(grant.id if grant is not None else None),
                        preferred_account_id=request.preferred_account_id,
                        inherited_account_id=request.inherited_account_id,
                    )
                )
                # A purpose selects one model.  Account/key failover happens
                # later inside this exact connection/model route; legacy
                # ordinal rows remain inert compatibility data and must never
                # become cross-model fallback candidates.
                break
            if not routes:
                raise ManagedOperationUnavailable(
                    f"no enabled {purpose} route supports {operation}"
                )
            return tuple(routes)
        finally:
            db.close()

    def _verify_input_artifacts(
        self,
        owner: str,
        artifacts: Iterable[OperationArtifact],
    ) -> tuple[OperationArtifact, ...]:
        result: list[OperationArtifact] = []
        names: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, OperationArtifact):
                raise ManagedOperationError("operation artifact descriptor is invalid")
            name = _required(artifact.name, "artifact name", identifier=True)
            if name in names:
                raise ManagedOperationError("operation artifact names must be unique")
            names.add(name)
            row = self.artifacts.get(owner=owner, artifact_id=artifact.artifact_id)
            self._verify_artifact_row(row, artifact)
            result.append(artifact)
        return tuple(result)

    async def _execute_engine(
        self,
        owner: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if self._executor is not None:
            return await self._executor(owner, payload)
        from src.model_dispatch import get_agent_supervisor

        supervisor = get_agent_supervisor()
        if supervisor is None:
            raise ManagedOperationUnavailable(
                "Open Clank managed engine is unavailable"
            )
        execute = getattr(supervisor, "execute_operation", None)
        if not callable(execute):
            raise ManagedOperationUnavailable(
                "Open Clank managed engine does not support typed operations"
            )
        result = await execute(owner, dict(payload))
        if not isinstance(result, Mapping):
            raise ManagedOperationProtocolError(
                "managed engine returned an invalid operation result"
            )
        return result

    async def execute(
        self,
        request: ManagedOperationRequest,
    ) -> ManagedOperationResult:
        owner = _owner(request.owner)
        operation = _required(request.operation, "operation")
        root_operation_id = _required(
            request.root_operation_id,
            "root operation ID",
            identifier=True,
        )
        idempotency_key = _required(
            request.idempotency_key,
            "idempotency key",
        )
        if not 16 <= len(idempotency_key) <= 128:
            raise ManagedOperationError(
                "idempotency key must contain between 16 and 128 characters"
            )
        if operation == "web.search" and not request.model_route_id:
            raise ManagedOperationDenied(
                "web.search requires an explicitly selected helper model route"
            )
        routes = self._resolve_routes(request)
        artifacts = self._verify_input_artifacts(owner, request.artifacts)
        payload = {
            "rootOperationID": root_operation_id,
            "idempotencyKey": idempotency_key,
            "operation": operation,
            "routes": [route.to_wire() for route in routes],
            "input": (
                _validate_search_input(request.input)
                if operation == "web.search"
                else _safe_json(request.input, label="operation input")
            ),
            "artifactInputs": [artifact.to_wire() for artifact in artifacts],
            "options": _safe_json(request.options, label="operation options"),
        }
        raw = await self._execute_engine(owner, payload)
        result = self._validate_result(
            owner=owner,
            request=request,
            routes=routes,
            raw=raw,
        )
        if operation == "web.search":
            search_usage = result.output.get("webSearchUsage")
            if isinstance(search_usage, Mapping):
                usage = dict(result.usage)
                usage.update({f"webSearch_{key}": value for key, value in search_usage.items()})
                result = replace(result, usage=usage)
        native_capacity = raw.get("quota") if isinstance(raw, Mapping) else None
        if native_capacity is not None:
            try:
                from services.stats.quota_adapters import native_capacity_envelope
                native_payload = native_capacity_envelope(
                    next(route for route in routes if route.model_route_id == result.model_route_id).provider_id,
                    native_capacity, observed_at=datetime.now(timezone.utc))
                if native_payload:
                    result = replace(result, output={**result.output, "_openclank_quota": native_payload})
            except Exception as error:
                logger.info("terminal quota metadata unavailable (%s)", type(error).__name__)
        quota_payload = result.output.get("_openclank_quota") if isinstance(result.output, Mapping) else None
        public_result = (replace(result, output={key: value for key, value in result.output.items()
                                                  if key != "_openclank_quota"})
                         if isinstance(result.output, Mapping) and "_openclank_quota" in result.output
                         else result)
        if bool(request.options.get("incognito")):
            return public_result
        # Capture the validated terminal result at the managed operation seam.
        # A Stats failure must not turn a committed operation into a host error.
        try:
            from services.stats.ledger import capture_operation_result
            db = self._session_factory()
            try:
                selected_route = next(route for route in routes if route.model_route_id == result.model_route_id)
                capture_operation_result(db, request, result, selected_route=selected_route)
                db.commit()
                if quota_payload is not None:
                    try:
                        from services.stats.quota_adapters import admit_terminal_quota_payload
                        admit_terminal_quota_payload(
                            db, owner=request.owner, operation_id=result.operation_id,
                            root_operation_id=result.root_operation_id,
                            connection_id=selected_route.connection_id,
                            provider_id=selected_route.provider_id,
                            billing_lane=selected_route.billing_lane,
                            account_id=result.selected_account_id,
                            payload=quota_payload)
                        db.commit()
                    except Exception as error:
                        db.rollback()
                        logger.warning("managed operation quota admission unavailable (%s)", type(error).__name__)
            finally:
                db.close()
        except Exception as error:
            logger.warning("managed operation Stats capture unavailable (%s)", type(error).__name__)
        return public_result

    def _validate_result(
        self,
        *,
        owner: str,
        request: ManagedOperationRequest,
        routes: Sequence[OperationRoute],
        raw: Mapping[str, Any],
    ) -> ManagedOperationResult:
        operation_id = _required(raw.get("operationID"), "operation result ID", identifier=True)
        root_id = _required(raw.get("rootOperationID"), "result root operation ID", identifier=True)
        operation = _required(raw.get("operation"), "result operation")
        state = str(raw.get("state") or "")
        if root_id != request.root_operation_id or operation != request.operation:
            raise ManagedOperationProtocolError(
                "managed engine returned a result for a different operation"
            )
        if state not in {"complete", "failed", "cancelled"}:
            raise ManagedOperationProtocolError(
                "managed engine returned a nonterminal operation result"
            )
        committed = raw.get("committed")
        replayed = raw.get("replayed")
        if not isinstance(committed, bool) or not isinstance(replayed, bool):
            raise ManagedOperationProtocolError(
                "managed engine omitted operation commitment metadata"
            )
        if state == "complete" and not committed:
            raise ManagedOperationProtocolError(
                "managed engine completed an operation before its commit barrier"
            )

        selected = (
            _required(raw.get("modelRouteID"), "selected model route ID"),
            _required(raw.get("connectionID"), "selected connection ID"),
            _required(raw.get("billingLane"), "selected billing lane"),
        )
        offered = {
            (route.model_route_id, route.connection_id, route.billing_lane)
            for route in routes
        }
        if selected not in offered:
            raise ManagedOperationProtocolError(
                "managed engine selected a route outside the authorized candidates"
            )

        output = (
            _validate_search_output(raw.get("output") or {})
            if operation == "web.search" and state == "complete"
            else _safe_json(raw.get("output") or {}, label="operation output")
        )
        if not isinstance(output, Mapping):
            raise ManagedOperationProtocolError(
                "managed operation output must be an object"
            )
        result_artifacts: list[OperationArtifact] = []
        for index, descriptor in enumerate(raw.get("artifacts") or ()):
            if not isinstance(descriptor, Mapping):
                raise ManagedOperationProtocolError(
                    "managed operation artifact descriptor is invalid"
                )
            artifact = OperationArtifact(
                name=str(descriptor.get("name") or f"output-{index}"),
                artifact_id=_required(descriptor.get("artifactID"), "result artifact ID"),
                content_sha256=_required(
                    descriptor.get("contentSHA256"),
                    "result artifact hash",
                ),
                size_bytes=int(descriptor.get("sizeBytes")),
                media_type=_required(
                    descriptor.get("mediaType"),
                    "result artifact media type",
                ),
            )
            row = self.artifacts.get(owner=owner, artifact_id=artifact.artifact_id)
            self._verify_artifact_row(row, artifact)
            result_artifacts.append(artifact)

        if state == "complete":
            if operation in _BINARY_OUTPUT_OPERATIONS and not result_artifacts:
                raise ManagedOperationProtocolError(
                    "binary model operation completed without an artifact"
                )
            if operation in _TEXT_OUTPUT_OPERATIONS and not isinstance(output.get("text"), str):
                raise ManagedOperationProtocolError(
                    "text model operation completed without text"
                )

        model_fingerprint = raw.get("modelFingerprint")
        dimension = raw.get("dimension")
        selected_account_raw = raw.get("selectedAccountID")
        if request.preferred_account_id and selected_account_raw != request.preferred_account_id:
            raise ManagedOperationProtocolError(
                "managed engine selected an account different from the bound request"
            )
        if operation == "embeddings.create" and state == "complete":
            vectors = output.get("embeddings")
            if not isinstance(vectors, list) or not vectors:
                raise ManagedOperationProtocolError(
                    "embedding operation completed without vectors"
                )
            try:
                dimension = int(dimension)
            except (TypeError, ValueError):
                raise ManagedOperationProtocolError(
                    "embedding operation omitted its dimension"
                ) from None
            requested_texts = request.input.get("texts")
            if (
                not isinstance(requested_texts, (list, tuple))
                or not requested_texts
                or len(vectors) != len(requested_texts)
            ):
                raise ManagedOperationProtocolError(
                    "embedding vectors do not match the requested row count"
                )
            if dimension < 1 or any(
                not isinstance(vector, list)
                or len(vector) != dimension
                or any(
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                    for value in vector
                )
                for vector in vectors
            ):
                raise ManagedOperationProtocolError(
                    "embedding vectors do not match their declared dimension"
                )
            model_fingerprint = _required(
                model_fingerprint,
                "embedding model fingerprint",
            )

        usage_raw = raw.get("usage") or {}
        if not isinstance(usage_raw, Mapping):
            raise ManagedOperationProtocolError("operation usage is invalid")
        normalization_profile = raw.get("normalizationProfile")
        if normalization_profile is not None:
            if (
                not isinstance(normalization_profile, str)
                or not normalization_profile
                or len(normalization_profile) > 128
                or any(ord(char) < 32 for char in normalization_profile)
            ):
                raise ManagedOperationProtocolError(
                    "operation normalization profile is invalid"
                )
        usage: dict[str, int] = {}
        for key in (
            "inputTokens", "outputTokens", "totalTokens",
            "cacheReadTokens", "cacheWriteTokens", "reasoningTokens",
        ):
            if key in usage_raw:
                raw_value = usage_raw[key]
                if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                    raise ManagedOperationProtocolError("operation usage counts must be integers")
                value = raw_value
                if value < 0:
                    raise ManagedOperationProtocolError(
                        "operation usage must not be negative"
                    )
                usage[key] = value

        return ManagedOperationResult(
            operation_id=operation_id,
            root_operation_id=root_id,
            operation=operation,
            state=state,
            committed=committed,
            replayed=replayed,
            model_route_id=selected[0],
            connection_id=selected[1],
            billing_lane=selected[2],
            output=dict(output),
            artifacts=tuple(result_artifacts),
            commit_reason=(str(raw["commitReason"]) if raw.get("commitReason") else None),
            binding_id=(str(raw["bindingID"]) if raw.get("bindingID") else None),
            selected_account_id=(
                str(raw["selectedAccountID"])
                if raw.get("selectedAccountID")
                else None
            ),
            usage=usage,
            normalization_profile=normalization_profile,
            model_fingerprint=(
                str(model_fingerprint) if model_fingerprint is not None else None
            ),
            dimension=(int(dimension) if dimension is not None else None),
        )


_default_router: Optional[ManagedOperationRouter] = None


def get_operation_router() -> ManagedOperationRouter:
    global _default_router
    if _default_router is None:
        _default_router = ManagedOperationRouter()
    return _default_router


def reset_operation_router_for_test() -> None:
    global _default_router
    _default_router = None


__all__ = [
    "MODEL_OPERATIONS",
    "PURPOSE_OPERATIONS",
    "ManagedOperationDenied",
    "ManagedOperationError",
    "ManagedOperationProtocolError",
    "ManagedOperationRequest",
    "ManagedOperationResult",
    "ManagedOperationRouter",
    "ManagedOperationUnavailable",
    "OperationArtifact",
    "OperationRoute",
    "get_operation_router",
    "reset_operation_router_for_test",
]

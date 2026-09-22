"""Generated Python binding for the Open Clank managed-engine contract.

Source: ``contracts/openclank/managed-provider-v1.schema.json``.  Keep the
schema digest synchronized through the contract-generation check; neither side
may silently negotiate a different credential or operation protocol.
"""

from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .generated.managed_provider_contract import (
    ENGINE_METHODS,
    HOST_CALLBACK_METHODS,
    MANAGED_METHODS,
    METHOD_DIRECTIONS,
    MODEL_OPERATIONS,
    OPERATION_ROUTER_VERSION,
    PROTOCOL_VERSION,
    PROVIDER_STORE_VERSION,
    SCHEMA_ID,
    SCHEMA_SHA256,
    SCHEMA_VERSION,
)


class ManagedProtocolError(RuntimeError):
    pass


class ManagedMethodValidationError(ManagedProtocolError):
    """A secret-free wire validation failure for one managed callback."""


_CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "openclank"
    / "managed-provider-v1.schema.json"
)


@lru_cache(maxsize=1)
def _contract() -> dict[str, Any]:
    return json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _method_validator(method: str, direction: str):
    """Compile one validator without ever formatting a secret-bearing value."""

    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:  # pragma: no cover - packaging invariant
        raise ManagedProtocolError(
            "managed provider schema validation is unavailable"
        ) from exc

    contract = _contract()
    mapping = contract.get("x-openclank-methods", {}).get(method)
    if not isinstance(mapping, dict):
        raise ManagedMethodValidationError("unsupported managed provider callback")
    target = mapping.get(direction)
    if target == "empty object":
        schema: dict[str, Any] = {
            "$schema": contract["$schema"],
            "type": "object",
            "maxProperties": 0,
        }
    elif isinstance(target, str) and target.startswith("#/"):
        schema = {
            "$schema": contract["$schema"],
            "$ref": target,
            "$defs": contract["$defs"],
        }
    else:
        raise ManagedMethodValidationError(
            "managed provider callback has no pinned wire schema"
        )
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_managed_method_payload(
    method: str,
    payload: Any,
    *,
    direction: str,
) -> dict[str, Any]:
    """Validate an exact callback envelope without echoing its contents."""

    if direction not in {"request", "result"}:
        raise ValueError("managed callback direction must be request or result")
    if not isinstance(payload, dict):
        raise ManagedMethodValidationError(
            f"managed provider callback {direction} must be an object"
        )
    validator = _method_validator(method, direction)
    error = next(iter(validator.iter_errors(payload)), None)
    if error is not None:
        path = ".".join(str(item) for item in error.absolute_path) or "$"
        raise ManagedMethodValidationError(
            f"managed provider callback {direction} does not match its "
            f"pinned schema at {path}"
        )
    if method == "_openclank/provider-control/v1/account/validate" and direction == "result":
        discovery = payload["discovery"]
        if (
            payload["accountID"] != discovery["accountID"]
            or payload["credentialRevision"] != discovery["credentialRevision"]
            or payload["modelRoutes"] != discovery["models"]
        ):
            raise ManagedMethodValidationError(
                "account validation result disagrees with its discovery result"
            )
    return payload


def validate_managed_method_request(method: str, payload: Any) -> dict[str, Any]:
    return validate_managed_method_payload(method, payload, direction="request")


def validate_managed_method_result(method: str, payload: Any) -> dict[str, Any]:
    return validate_managed_method_payload(method, payload, direction="result")


def validate_engine_method_request(method: str, payload: Any) -> dict[str, Any]:
    if method not in ENGINE_METHODS:
        raise ManagedMethodValidationError("unsupported managed engine method")
    return validate_managed_method_request(method, payload)


def validate_engine_method_result(method: str, payload: Any) -> dict[str, Any]:
    if method not in ENGINE_METHODS:
        raise ManagedMethodValidationError("unsupported managed engine method")
    return validate_managed_method_result(method, payload)


class ManagedCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    protocolVersion: int
    providerStoreVersion: int
    operationRouterVersion: int
    schemaID: str
    schemaVersion: int
    schemaHash: str = Field(pattern=r"^[a-f0-9]{64}$")
    methods: list[str]
    operations: list[str]
    artifactTransfer: bool
    localExecutor: bool


def client_capability_offer() -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "providerStoreVersion": PROVIDER_STORE_VERSION,
        "operationRouterVersion": OPERATION_ROUTER_VERSION,
        "schemaID": SCHEMA_ID,
        "schemaVersion": SCHEMA_VERSION,
        "schemaHash": SCHEMA_SHA256,
        "methods": sorted(MANAGED_METHODS),
        "operations": sorted(MODEL_OPERATIONS),
        "artifactTransfer": True,
        "localExecutor": True,
    }


def validate_initialize_result(result: dict[str, Any]) -> ManagedCapabilities:
    if not isinstance(result, dict):
        raise ManagedProtocolError("managed engine initialize result is not an object")
    metadata = result.get("_meta")
    declaration = metadata.get("openclankManaged") if isinstance(metadata, dict) else None
    try:
        capabilities = ManagedCapabilities.model_validate(declaration)
    except ValidationError as exc:
        raise ManagedProtocolError("managed engine did not declare a valid Open Clank contract") from exc
    exact = {
        "protocolVersion": PROTOCOL_VERSION,
        "providerStoreVersion": PROVIDER_STORE_VERSION,
        "operationRouterVersion": OPERATION_ROUTER_VERSION,
        "schemaID": SCHEMA_ID,
        "schemaVersion": SCHEMA_VERSION,
        "schemaHash": SCHEMA_SHA256,
        "artifactTransfer": True,
        "localExecutor": True,
    }
    for field, expected in exact.items():
        if getattr(capabilities, field) != expected:
            raise ManagedProtocolError(
                f"managed engine {field} is incompatible: expected {expected!r}"
            )
    method_set = set(capabilities.methods)
    operation_set = set(capabilities.operations)
    if len(method_set) != len(capabilities.methods) or method_set != MANAGED_METHODS:
        missing = MANAGED_METHODS - method_set
        unexpected = method_set - MANAGED_METHODS
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            detail.append("unexpected " + ", ".join(sorted(unexpected)))
        if len(method_set) != len(capabilities.methods):
            detail.append("duplicate callback declarations")
        raise ManagedProtocolError(
            "managed engine callback set is incompatible: " + "; ".join(detail)
        )
    if len(operation_set) != len(capabilities.operations) or operation_set != MODEL_OPERATIONS:
        missing = MODEL_OPERATIONS - operation_set
        unexpected = operation_set - MODEL_OPERATIONS
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if unexpected:
            detail.append("unexpected " + ", ".join(sorted(unexpected)))
        if len(operation_set) != len(capabilities.operations):
            detail.append("duplicate operation declarations")
        raise ManagedProtocolError(
            "managed engine operation set is incompatible: " + "; ".join(detail)
        )
    return capabilities


__all__ = [
    "ENGINE_METHODS",
    "HOST_CALLBACK_METHODS",
    "MANAGED_METHODS",
    "METHOD_DIRECTIONS",
    "MODEL_OPERATIONS",
    "ManagedCapabilities",
    "ManagedMethodValidationError",
    "ManagedProtocolError",
    "SCHEMA_SHA256",
    "client_capability_offer",
    "validate_managed_method_payload",
    "validate_managed_method_request",
    "validate_managed_method_result",
    "validate_initialize_result",
    "validate_engine_method_request",
    "validate_engine_method_result",
]

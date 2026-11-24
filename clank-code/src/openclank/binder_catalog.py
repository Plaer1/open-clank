"""Validated, account-scoped Binder selection metadata; never an access grant."""
from __future__ import annotations

import re
import unicodedata
import copy
import hashlib
import json
import uuid
from typing import Any


class BinderCatalogError(ValueError):
    def __init__(self, message: str, code: str = "invalid_catalog"):
        super().__init__(message)
        self.code = code


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WORKSPACE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _text(value: Any, label: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value) or len(value.encode("utf-8")) > limit:
        raise BinderCatalogError(f"Invalid {label}")
    return value


def _revision(value: Any) -> int:
    if type(value) is not int or value < 0 or value > 2**53 - 1:
        raise BinderCatalogError("Invalid revision")
    return value


def empty_catalog(account_id: str) -> dict[str, Any]:
    return {"schemaVersion": 1, "accountId": _text(account_id, "account"), "revision": 0, "configuredIds": [], "defaultId": None, "descriptors": {}}


def validate_catalog(value: Any, account_id: str) -> dict[str, Any]:
    keys = {"schemaVersion", "accountId", "revision", "configuredIds", "defaultId", "descriptors"}
    if not isinstance(value, dict) or set(value) != keys or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise BinderCatalogError("Unsupported or invalid Binder catalog")
    if value["accountId"] != _text(account_id, "account"):
        raise BinderCatalogError("Binder catalog belongs to another account", "account_mismatch")
    revision = _revision(value["revision"])
    configured, descriptors = value["configuredIds"], value["descriptors"]
    if not isinstance(configured, list) or len(configured) > 128 or not isinstance(descriptors, dict) or len(descriptors) > 128:
        raise BinderCatalogError("Invalid Binder collection")
    if any(not isinstance(item, str) or not _ID.fullmatch(item) for item in configured) or len(set(configured)) != len(configured):
        raise BinderCatalogError("Invalid configured Binder IDs")
    clean: dict[str, Any] = {}
    sources: set[tuple[Any, ...]] = set()
    for binder_id, descriptor in descriptors.items():
        if not isinstance(binder_id, str) or not _ID.fullmatch(binder_id) or not isinstance(descriptor, dict) or set(descriptor) != {"id", "displayName", "provider", "source", "enabled", "revision"} or descriptor["id"] != binder_id:
            raise BinderCatalogError("Invalid Binder descriptor")
        if descriptor["provider"] != "copal":
            raise BinderCatalogError("Binder provider is unavailable", "provider_unavailable")
        source = descriptor["source"]
        if not isinstance(source, dict) or set(source) != {"workspaceId", "corpus", "root"} or not isinstance(source["corpus"], str) or source["corpus"] not in {"notes", "wiki"} or not isinstance(source["root"], list) or len(source["root"]) > 64:
            raise BinderCatalogError("Invalid Binder source")
        workspace = _text(source["workspaceId"], "workspace")
        if not _WORKSPACE.fullmatch(workspace):
            raise BinderCatalogError("Invalid Copal workspace")
        parts = []
        for part in source["root"]:
            part = _text(part, "folder segment")
            if part in {".", ".."} or "/" in part or "\\" in part:
                raise BinderCatalogError("Invalid logical folder segment")
            parts.append(part)
        identity = (unicodedata.normalize("NFC", workspace).casefold(), source["corpus"], *(unicodedata.normalize("NFC", part).casefold() for part in parts))
        if identity in sources:
            raise BinderCatalogError("Duplicate Binder source")
        sources.add(identity)
        if type(descriptor["enabled"]) is not bool:
            raise BinderCatalogError("Invalid Binder enabled state")
        clean[binder_id] = {"id": binder_id, "displayName": _text(descriptor["displayName"], "display name"), "provider": "copal", "source": {"workspaceId": workspace, "corpus": source["corpus"], "root": parts}, "enabled": descriptor["enabled"], "revision": _revision(descriptor["revision"])}
    if any(item not in clean for item in configured):
        raise BinderCatalogError("Configured Binder is missing")
    default = value["defaultId"]
    if default is not None and (not isinstance(default, str) or default not in configured or not clean[default]["enabled"]):
        raise BinderCatalogError("Default Binder is not enabled and configured")
    return {"schemaVersion": 1, "accountId": account_id, "revision": revision, "configuredIds": list(configured), "defaultId": default, "descriptors": clean}


def prepare_update(stored: Any, incoming: Any, account_id: str, expected_revision: Any) -> dict[str, Any]:
    current = empty_catalog(account_id) if stored is None else validate_catalog(stored, account_id)
    if _revision(expected_revision) != current["revision"]:
        raise BinderCatalogError("Binder catalog changed", "revision_conflict")
    proposed = validate_catalog(incoming, account_id)
    if proposed["revision"] != current["revision"]:
        raise BinderCatalogError("Binder catalog changed", "revision_conflict")
    if current["revision"] == 2**53 - 1:
        raise BinderCatalogError("Binder catalog revision is exhausted")
    for binder_id, descriptor in proposed["descriptors"].items():
        old = current["descriptors"].get(binder_id)
        expected = old["revision"] if old else 0
        if descriptor["revision"] != expected:
            raise BinderCatalogError("Binder descriptor changed", "revision_conflict")
        changed = old is None or {k: v for k, v in descriptor.items() if k != "revision"} != {k: v for k, v in old.items() if k != "revision"}
        if changed and expected == 2**53 - 1:
            raise BinderCatalogError("Binder descriptor revision is exhausted")
        descriptor["revision"] = expected + int(changed)
    proposed["revision"] = current["revision"] + 1
    return proposed


def storage_parts(stored: Any, account_id: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Project private conversion receipts without changing the public v1 DTO."""
    if stored is None:
        return None, {}
    if isinstance(stored, dict) and stored.get("schemaVersion") == 2:
        if set(stored) not in ({"schemaVersion", "catalog", "operations"}, {"schemaVersion", "catalog", "operations", "revision"}) or not isinstance(stored["operations"], dict) or len(stored["operations"]) > 32:
            raise BinderCatalogError("Unsupported Binder receipt envelope", "provider_unavailable")
        catalog = None if stored["catalog"] is None else validate_catalog(stored["catalog"], account_id)
        operations = copy.deepcopy(stored["operations"])
        for key, receipt in operations.items():
            try:
                if str(uuid.UUID(key)) != key or set(receipt) != {"requestDigest", "before", "after", "state"} or receipt["state"] not in {"pending", "applied", "rolled_back", "needs_review"} or not re.fullmatch(r"[0-9a-f]{64}", receipt["requestDigest"]):
                    raise ValueError()
                if receipt["before"] is not None:
                    validate_catalog(receipt["before"], account_id)
                validate_catalog(receipt["after"], account_id)
            except (ValueError, TypeError, AttributeError):
                raise BinderCatalogError("Invalid Binder receipt", "provider_unavailable") from None
        if "revision" in stored:
            revision = _revision(stored["revision"])
            if catalog is not None and catalog["revision"] != revision:
                raise BinderCatalogError("Invalid Binder envelope revision", "provider_unavailable")
        return catalog, operations
    return validate_catalog(stored, account_id), {}


def public_catalog(stored: Any, account_id: str) -> dict[str, Any]:
    catalog, operations = storage_parts(stored, account_id)
    if catalog is not None:
        return catalog
    empty = empty_catalog(account_id)
    if isinstance(stored, dict) and stored.get("schemaVersion") == 2:
        inferred = max((item["after"]["revision"] + int(item["state"] == "rolled_back") for item in operations.values()), default=0)
        empty["revision"] = _revision(stored.get("revision", inferred))
    return empty


def store_catalog(stored: Any, catalog: dict[str, Any] | None, account_id: str) -> Any:
    _, operations = storage_parts(stored, account_id)
    validated = None if catalog is None else validate_catalog(catalog, account_id)
    revision = validated["revision"] if validated is not None else public_catalog(stored, account_id)["revision"]
    return {"schemaVersion":2, "catalog":validated, "revision":revision, "operations":operations} if operations or isinstance(stored, dict) and stored.get("schemaVersion") == 2 else validated


def operation_digest(request: Any) -> str:
    return hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def prepare_storage_update(stored: Any, incoming: Any, account_id: str, expected_revision: int) -> dict[str, Any]:
    current = public_catalog(stored, account_id)
    _, operations = storage_parts(stored, account_id)
    candidate = prepare_update(current, incoming, account_id, expected_revision)
    new_ids = set(candidate["descriptors"]) - set(current["descriptors"])
    retired = {binder_id for receipt in operations.values() for snapshot in (receipt["before"], receipt["after"]) if snapshot for binder_id in snapshot["descriptors"]}
    if new_ids & retired:
        raise BinderCatalogError("A retired Binder ID cannot be reused", "revision_conflict")
    return candidate


def apply_conversion(stored: Any, *, account_id: str, operation_id: str, request: dict[str, Any], incoming: dict[str, Any], expected_revision: int) -> tuple[Any, dict[str, Any]]:
    if not isinstance(request, dict) or request.get("accountId") != account_id or request.get("expectedRevision") != expected_revision or request.get("catalog") != incoming:
        raise BinderCatalogError("Conversion request does not match its catalog", "revision_conflict")
    try:
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise BinderCatalogError("Invalid conversion operation ID") from None
    before, operations = storage_parts(stored, account_id)
    digest = operation_digest(request)
    if operation_id in operations:
        receipt = operations[operation_id]
        if receipt["requestDigest"] != digest:
            raise BinderCatalogError("Conversion operation payload changed", "revision_conflict")
        return stored, copy.deepcopy(receipt)
    if len(operations) >= 32:
        raise BinderCatalogError("Binder conversion receipt capacity is exhausted", "provider_unavailable")
    after = prepare_storage_update(stored, incoming, account_id, expected_revision)
    receipt = {"requestDigest":digest, "before":copy.deepcopy(before), "after":after, "state":"pending"}
    operations[operation_id] = receipt
    return {"schemaVersion":2, "catalog":after, "revision":after["revision"], "operations":operations}, copy.deepcopy(receipt)


def rollback_conversion(stored: Any, *, account_id: str, operation_id: str) -> tuple[Any, dict[str, Any]]:
    current, operations = storage_parts(stored, account_id)
    receipt = operations.get(operation_id)
    if not receipt:
        raise BinderCatalogError("Conversion operation is unavailable", "revision_conflict")
    if receipt["state"] == "rolled_back":
        return stored, copy.deepcopy(receipt)
    if current != receipt["after"]:
        raise BinderCatalogError("Binder catalog changed after conversion", "revision_conflict")
    revision = public_catalog(stored, account_id)["revision"]
    if revision == 2**53 - 1:
        raise BinderCatalogError("Binder catalog revision is exhausted")
    restored = copy.deepcopy(receipt["before"])
    if restored is not None:
        restored["revision"] = revision + 1
        for binder_id, descriptor in restored["descriptors"].items():
            old = current["descriptors"].get(binder_id) if current else None
            if old and {k:v for k,v in old.items() if k != "revision"} == {k:v for k,v in descriptor.items() if k != "revision"}:
                descriptor["revision"] = old["revision"]
                continue
            revisions = [descriptor["revision"], old["revision"] if old else 0]
            for previous in operations.values():
                for snapshot in (previous["before"], previous["after"]):
                    if snapshot and binder_id in snapshot["descriptors"]:
                        revisions.append(snapshot["descriptors"][binder_id]["revision"])
            maximum = max(revisions)
            if maximum == 2**53 - 1:
                raise BinderCatalogError("Binder descriptor revision is exhausted")
            descriptor["revision"] = maximum + 1
    receipt["state"] = "rolled_back"
    return {"schemaVersion":2, "catalog":restored, "revision":revision + 1, "operations":operations}, copy.deepcopy(receipt)


def mark_conversion_review(stored: Any, *, account_id: str, operation_id: str) -> tuple[Any, dict[str, Any]]:
    current, operations = storage_parts(stored, account_id)
    receipt = operations.get(operation_id)
    if not receipt:
        raise BinderCatalogError("Conversion operation is unavailable", "revision_conflict")
    if receipt["state"] in {"pending", "applied"}:
        receipt["state"] = "needs_review"
    return {"schemaVersion":2, "catalog":current, "revision":public_catalog(stored, account_id)["revision"], "operations":operations}, copy.deepcopy(receipt)


def settle_conversion(stored: Any, *, account_id: str, operation_id: str) -> tuple[Any, dict[str, Any]]:
    current, operations = storage_parts(stored, account_id)
    receipt = operations.get(operation_id)
    if not receipt:
        raise BinderCatalogError("Conversion operation is unavailable", "revision_conflict")
    if receipt["state"] == "pending":
        receipt["state"] = "applied" if current == receipt["after"] else "needs_review"
    return {"schemaVersion":2, "catalog":current, "revision":public_catalog(stored, account_id)["revision"], "operations":operations}, copy.deepcopy(receipt)

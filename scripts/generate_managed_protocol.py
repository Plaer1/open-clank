#!/usr/bin/env python3
"""Generate and check bindings for the managed host/engine contract.

The JSON schema is the only source of method, operation, direction, and digest
metadata.  Python and TypeScript consumers import the generated constants; the
check mode also verifies every recorded digest consumer, including build and
catalogue provenance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SCHEMA = ROOT / "contracts/openclank/managed-provider-v2.schema.json"
PYTHON_OUTPUT = ROOT / "src/openclank/generated/managed_provider_contract.py"
TYPESCRIPT_OUTPUT = ROOT / "packages/mimo-code/packages/opencode/src/acp/generated/openclank-managed-contract.ts"
VENDOR_MANIFEST = ROOT / "packages/mimo-code/openclank-vendor.json"
PROVENANCE_SCHEMA = ROOT / "contracts/openclank/engine-provenance-v1.schema.json"
FAMILY_CATALOG = ROOT / "src/openclank/provider_family_catalog.py"


def _load_schema() -> tuple[dict[str, Any], str]:
    source = SCHEMA.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    schema = json.loads(source)
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
    except ImportError as exc:  # pragma: no cover - environment invariant
        raise RuntimeError("jsonschema is required for managed protocol generation") from exc
    if schema.get("$id") != "https://openclank.dev/contracts/managed-provider/v2":
        raise RuntimeError("managed protocol generator requires the v2 contract")
    schema_version = (
        schema.get("$defs", {})
        .get("capabilityDeclaration", {})
        .get("properties", {})
        .get("schemaVersion", {})
        .get("const")
    )
    if schema_version != 2:
        raise RuntimeError("managed protocol generator requires capability schemaVersion 2")
    return schema, digest


def _bindings(schema: dict[str, Any], digest: str) -> dict[str, Any]:
    defs = schema.get("$defs")
    if not isinstance(defs, dict):
        raise RuntimeError("managed schema is missing $defs")
    methods = defs.get("managedMethod", {}).get("enum")
    operations = defs.get("operation", {}).get("enum")
    mapping = schema.get("x-openclank-methods")
    if not isinstance(methods, list) or not isinstance(operations, list) or not isinstance(mapping, dict):
        raise RuntimeError("managed schema is missing method or operation metadata")
    if len(methods) != 25 or len(set(methods)) != 25:
        raise RuntimeError(f"managed contract must contain 25 unique methods; found {len(methods)}")
    if len(operations) != 16 or len(set(operations)) != 16:
        raise RuntimeError(f"managed contract must contain 16 unique operations; found {len(operations)}")
    if set(mapping) != set(methods):
        raise RuntimeError("managed method wire mappings do not cover the exact method set")

    provider_store = [method for method in methods if method.startswith("_openclank/provider-store/")]
    provider_control = [method for method in methods if method.startswith("_openclank/provider-control/")]
    operation_methods = [method for method in methods if method.startswith("_openclank/operations/")]
    session_methods = [method for method in methods if method.startswith("_openclank/session/")]
    if (len(provider_store), len(provider_control), len(operation_methods), len(session_methods)) != (9, 7, 6, 3):
        raise RuntimeError("managed method families must contain 9 store, 7 control, 6 operation, and 3 session methods")
    directions = {method: mapping[method].get("direction") for method in methods}
    if any(direction not in {"engine_to_host", "host_to_engine"} for direction in directions.values()):
        raise RuntimeError("managed method wire mappings must declare a valid callback direction")
    engine_methods = [method for method in methods if directions[method] == "host_to_engine"]
    expected_engine_methods = provider_control + ["_openclank/operations/v1/execute", "_openclank/operations/v1/cancel", "_openclank/session/v1/settings/effective"]
    if set(engine_methods) != set(expected_engine_methods):
        raise RuntimeError("managed callback directions do not preserve host engine authority")
    return {
        "schema_id": schema.get("$id"),
        "schema_version": defs.get("capabilityDeclaration", {}).get("properties", {}).get("schemaVersion", {}).get("const"),
        "digest": digest,
        "methods": methods,
        "provider_store_methods": provider_store,
        "provider_control_methods": provider_control,
        "operation_methods": operation_methods,
        "session_methods": session_methods,
        "engine_methods": engine_methods,
        "host_callback_methods": [method for method in methods if method not in engine_methods],
        "operations": operations,
        "directions": directions,
        "wire_mappings": mapping,
    }


def _py_text(data: dict[str, Any]) -> str:
    def tuple_literal(values: list[str]) -> str:
        return "(" + ",\n".join(f"    {value!r}" for value in values) + ",\n)"

    directions = "{\n" + ",\n".join(f"    {key!r}: {value!r}" for key, value in data["directions"].items()) + "\n}"
    mappings = "{\n" + ",\n".join(
        f"    {key!r}: ({value['request']!r}, {value['result']!r})"
        for key, value in data["wire_mappings"].items()
    ) + "\n}"
    return f'''"""Generated managed-provider contract constants; edit the JSON schema instead."""

SCHEMA_ID = {data["schema_id"]!r}
SCHEMA_VERSION = {data["schema_version"]!r}
SCHEMA_SHA256 = {data["digest"]!r}
PROTOCOL_VERSION = 1
PROVIDER_STORE_VERSION = 1
OPERATION_ROUTER_VERSION = 1

PROVIDER_STORE_METHODS = {tuple_literal(data["provider_store_methods"])}
PROVIDER_CONTROL_METHODS = {tuple_literal(data["provider_control_methods"])}
OPERATION_METHODS = {tuple_literal(data["operation_methods"])}
SESSION_METHODS = {tuple_literal(data["session_methods"])}
MANAGED_METHODS = frozenset({tuple_literal(data["methods"])})
ENGINE_METHODS = frozenset({tuple_literal(data["engine_methods"])})
HOST_CALLBACK_METHODS = frozenset({tuple_literal(data["host_callback_methods"])})
MODEL_OPERATIONS = frozenset({tuple_literal(data["operations"])})
METHOD_DIRECTIONS = {directions}
METHOD_WIRE_MAPPINGS = {mappings}
'''


def _ts_text(data: dict[str, Any]) -> str:
    def array(values: list[str]) -> str:
        return "[\n" + "\n".join(f'  {json.dumps(value)},' for value in values) + "\n] as const"

    directions = "{\n" + "\n".join(f'  {json.dumps(key)}: {json.dumps(value)},' for key, value in data["directions"].items()) + "\n} as const"
    return f'''// Generated managed-provider contract constants; edit the JSON schema instead.
export const PROTOCOL_VERSION = 1 as const
export const PROVIDER_STORE_VERSION = 1 as const
export const OPERATION_ROUTER_VERSION = 1 as const
export const SCHEMA_VERSION = {data["schema_version"]} as const
export const SCHEMA_ID = {json.dumps(data["schema_id"])} as const
export const SCHEMA_HASH = {json.dumps(data["digest"])} as const

export const PROVIDER_STORE_METHODS = {array(data["provider_store_methods"])}
export const PROVIDER_CONTROL_METHODS = {array(data["provider_control_methods"])}
export const OPERATION_METHODS = {array(data["operation_methods"])}
export const SESSION_METHODS = {array(data["session_methods"])}
export const OPERATIONS = {array(data["operations"])}
export const METHOD_DIRECTIONS = {directions}

export type ProviderStoreMethod = (typeof PROVIDER_STORE_METHODS)[number]
export type ProviderControlMethod = (typeof PROVIDER_CONTROL_METHODS)[number]
export type OperationMethod = (typeof OPERATION_METHODS)[number]
export type SessionMethod = (typeof SESSION_METHODS)[number]
export type ManagedMethod = ProviderStoreMethod | ProviderControlMethod | OperationMethod | SessionMethod
export type Operation = (typeof OPERATIONS)[number]
'''


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _update_digest_consumers(data: dict[str, Any]) -> None:
    # The managed protocol binding is part of the vendored engine source tree;
    # refresh its packaging fingerprint whenever generated source changes.
    from src.openclank.engine_build import source_fingerprint

    source_digest = source_fingerprint(VENDOR_MANIFEST.parent)
    vendor = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    vendor["managed_schema"] = {
        "id": data["schema_id"],
        "version": data["schema_version"],
        "repository_path": "contracts/openclank/managed-provider-v2.schema.json",
        "sha256": data["digest"],
    }
    vendor["build"]["source_sha256"] = source_digest
    _write(VENDOR_MANIFEST, json.dumps(vendor, indent=2) + "\n")

    provenance = PROVENANCE_SCHEMA.read_text(encoding="utf-8")
    provenance = re.sub(
        r'("id": \{ "const": ")[^"]+(" \})',
        rf"\g<1>{data['schema_id']}\g<2>",
        provenance,
        count=1,
    )
    provenance = re.sub(
        r'("version": \{ "const": )\d+( \})',
        rf"\g<1>{data['schema_version']}\g<2>",
        provenance,
        count=1,
    )
    provenance = re.sub(
        r'("sha256": \{ "const": ")[0-9a-f]{64}(" \})',
        rf"\g<1>{data['digest']}\g<2>",
        provenance,
        count=1,
    )
    _write(PROVENANCE_SCHEMA, provenance)

    family = FAMILY_CATALOG.read_text(encoding="utf-8")
    family, count = re.subn(r'(_MANAGED_SCHEMA_VERSION = )\d+', rf"\g<1>{data['schema_version']}", family)
    if count != 1:
        raise RuntimeError("provider family catalogue managed-schema version marker is missing")
    family, count = re.subn(r'(_MANAGED_SCHEMA_SHA256 = ")[0-9a-f]{64}(")', rf"\g<1>{data['digest']}\g<2>", family)
    if count != 1:
        raise RuntimeError("provider family catalogue managed-schema digest marker is missing")
    family, count = re.subn(r'(_ENGINE_SOURCE_SHA256 = ")[0-9a-f]{64}(")', rf"\g<1>{source_digest}\g<2>", family)
    if count != 1:
        raise RuntimeError("provider family catalogue engine-source digest marker is missing")
    _write(FAMILY_CATALOG, family)


def _check_digest_consumers(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    vendor = json.loads(VENDOR_MANIFEST.read_text(encoding="utf-8"))
    if vendor.get("managed_schema") != {
        "id": data["schema_id"],
        "version": data["schema_version"],
        "repository_path": "contracts/openclank/managed-provider-v2.schema.json",
        "sha256": data["digest"],
    }:
        errors.append(str(VENDOR_MANIFEST.relative_to(ROOT)))
    from src.openclank.engine_build import source_fingerprint

    source_digest = source_fingerprint(VENDOR_MANIFEST.parent)
    if vendor.get("build", {}).get("source_sha256") != source_digest:
        errors.append(str(VENDOR_MANIFEST.relative_to(ROOT)))
    provenance = PROVENANCE_SCHEMA.read_text(encoding="utf-8")
    if (
        f'"id": {{ "const": "{data["schema_id"]}" }}' not in provenance
        or f'"version": {{ "const": {data["schema_version"]} }}' not in provenance
        or f'"sha256": {{ "const": "{data["digest"]}" }}' not in provenance
    ):
        errors.append(str(PROVENANCE_SCHEMA.relative_to(ROOT)))
    family = FAMILY_CATALOG.read_text(encoding="utf-8")
    if f'_MANAGED_SCHEMA_VERSION = {data["schema_version"]}' not in family:
        errors.append(str(FAMILY_CATALOG.relative_to(ROOT)))
    if f'_MANAGED_SCHEMA_SHA256 = "{data["digest"]}"' not in family:
        errors.append(str(FAMILY_CATALOG.relative_to(ROOT)))
    if f'_ENGINE_SOURCE_SHA256 = "{source_digest}"' not in family:
        errors.append(str(FAMILY_CATALOG.relative_to(ROOT)))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="write deterministic bindings and digest consumers")
    mode.add_argument("--check", action="store_true", help="check bindings and digest consumers without writing")
    args = parser.parse_args()
    schema, digest = _load_schema()
    data = _bindings(schema, digest)
    expected_python = _py_text(data)
    expected_typescript = _ts_text(data)
    if args.write:
        _write(PYTHON_OUTPUT, expected_python)
        _write(TYPESCRIPT_OUTPUT, expected_typescript)
        _update_digest_consumers(data)
        return 0

    errors: list[str] = []
    if not PYTHON_OUTPUT.exists() or PYTHON_OUTPUT.read_text(encoding="utf-8") != expected_python:
        errors.append(str(PYTHON_OUTPUT.relative_to(ROOT)))
    if not TYPESCRIPT_OUTPUT.exists() or TYPESCRIPT_OUTPUT.read_text(encoding="utf-8") != expected_typescript:
        errors.append(str(TYPESCRIPT_OUTPUT.relative_to(ROOT)))
    errors.extend(_check_digest_consumers(data))
    if errors:
        print("managed protocol generated output drift: " + ", ".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

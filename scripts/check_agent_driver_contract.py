#!/usr/bin/env python3
"""Validate the checked-in direct-driver schema and cross-language wire shape."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "contracts/openclank/agent-driver-v1.schema.json"
RESPONSE_SCHEMA = ROOT / "contracts/openclank/agent-driver-response-v1.schema.json"
FIXTURES = ROOT / "contracts/openclank/agent-driver-v1/fixtures"
RESPONSE_FIXTURES = FIXTURES / "responses"
TS_PROTOCOL = ROOT / "packages/mimo-code/packages/opencode/src/openclank-runtime/protocol.ts"
RUST_WIRE = ROOT / "packages/openclank-agent-supervisor/src/driver_wire.rs"


def _load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        schema = _load(SCHEMA)
        response_schema = _load(RESPONSE_SCHEMA)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.Draft202012Validator.check_schema(response_schema)
        files = sorted(FIXTURES.glob("*.json"))
        if not files:
            raise ValueError("direct-driver fixture corpus is empty")
        commands: set[str] = set()
        validator = jsonschema.Draft202012Validator(schema)
        for path in files:
            fixture = _load(path)
            validator.validate(fixture)
            commands.add(fixture["command"])
            encoded = json.dumps(fixture, separators=(",", ":")).encode()
            if len(encoded) > 256 * 1024:
                raise ValueError(f"fixture exceeds the 256 KiB wire bound: {path.name}")
            forbidden = {"credential", "password", "secret", "raw_path", "cwd"}
            if forbidden & fixture.keys() or forbidden & fixture["payload"].keys():
                raise ValueError(f"fixture contains a forbidden transport field: {path.name}")
        if "hello" not in commands or "submit_turn" not in commands:
            raise ValueError("fixture corpus must cover hello and submit_turn")
        response_validator = jsonschema.Draft202012Validator(response_schema)
        response_files = sorted(RESPONSE_FIXTURES.glob("*.json"))
        if not response_files:
            raise ValueError("direct-driver response fixture corpus is empty")
        response_shapes = set()
        for path in response_files:
            response = _load(path)
            response_validator.validate(response)
            response_shapes.add(response["ok"])
            if response.get("ok") and response.get("payload", {}).keys() & {"credential", "password", "secret", "raw_path", "cwd"}:
                raise ValueError(f"response fixture contains a forbidden field: {path.name}")
        if response_shapes != {True, False}:
            raise ValueError("response fixtures must cover success and typed error")
        ts = TS_PROTOCOL.read_text()
        if not re.search(r"DRIVER_PROTOCOL_MAJOR\s*=\s*1", ts):
            raise ValueError("TypeScript protocol major drift")
        if not re.search(r"MAX_DRIVER_ENVELOPE_BYTES\s*=\s*256\s*\*\s*1024", ts):
            raise ValueError("TypeScript envelope bound drift")
        rust = RUST_WIRE.read_text()
        if "pub const MAX_DRIVER_ENVELOPE_BYTES: usize = 256 * 1024;" not in rust:
            raise ValueError("Rust envelope bound drift")
    except (OSError, ValueError, json.JSONDecodeError, jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        print(f"agent-driver contract invalid: {exc}", file=sys.stderr)
        return 2
    print(f"agent-driver contract valid: {len(files)} request fixtures, {len(response_files)} response fixtures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

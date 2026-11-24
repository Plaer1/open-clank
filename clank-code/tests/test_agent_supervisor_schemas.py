import json
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts/openclank/agent-supervisor-v1"


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA_DIR / name).read_text())


def test_semantic_event_schema_accepts_bounded_fixture_and_rejects_secret_fields():
    schema = load_schema("semantic-event-v1.schema.json")
    event = {
        "schema_version": 1,
        "owner_subject_id": "subject-1",
        "session_id": "session-1",
        "runtime_id": "runtime-1",
        "runtime_epoch": "0123456789abcdef0123456789abcdef",
        "runtime_generation": 1,
        "run_id": "run-1",
        "turn_id": "turn-1",
        "semantic_epoch": "fedcba9876543210fedcba9876543210",
        "seq": 1,
        "emitted_unix_ms": 1,
        "op": "message_upsert",
        "entity_kind": "message",
        "entity_id": "message-1",
        "payload": {"text": "hello"},
        "payload_sha256": "0" * 64,
    }
    jsonschema.validate(event, schema)
    event["credential"] = "must-not-cross-boundary"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(event, schema)


def test_terminal_control_schema_requires_epoch_and_lease_for_mutations():
    schema = load_schema("terminal-control-v1.schema.json")
    control = {
        "schema_version": 1,
        "request_id": "a" * 32,
        "session_id": "session-1",
        "terminal_id": "b" * 32,
        "command": "input",
        "terminal_epoch": "c" * 32,
        "lease_id": "d" * 32,
        "input_b64": "AQI=",
    }
    jsonschema.validate(control, schema)
    del control["lease_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(control, schema)


def test_driver_schema_rejects_credentials_and_accepts_owner_bound_turn():
    schema = load_schema("../agent-driver-v1.schema.json")
    envelope = {
        "schema_version": 1,
        "request_id": "a" * 32,
        "owner_subject_id": "subject-1",
        "session_id": "session-1",
        "runtime_id": "runtime-1",
        "runtime_epoch": "b" * 32,
        "runtime_generation": 2,
        "command": "submit_turn",
        "payload": {"text": "hello"},
    }
    jsonschema.validate(envelope, schema)
    envelope["credential"] = "must-not-cross-boundary"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, schema)


def test_driver_response_schema_is_typed_and_secret_free():
    schema = load_schema("../agent-driver-response-v1.schema.json")
    jsonschema.validate(
        {
            "schema_version": 1,
            "request_id": "a" * 32,
            "ok": False,
            "error": {
                "code": "driver_not_activated",
                "safe_message": "host activation is required",
                "retryable": True,
            },
        },
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(
            {
                "schema_version": 1,
                "request_id": "a" * 32,
                "ok": True,
                "event": "hello_ack",
                "payload": {},
                "credential": "must-not-cross-boundary",
            },
            schema,
        )
    mixed = {
        "schema_version": 1,
        "request_id": "a" * 32,
        "ok": True,
        "event": "hello_ack",
        "payload": {},
        "error": {
            "code": "bad",
            "safe_message": "mixed",
            "retryable": False,
        },
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(mixed, schema)

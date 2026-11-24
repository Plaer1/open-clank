import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


SCHEMA = json.loads((Path(__file__).parents[1] / "contracts/openclank/managed-provider-v2.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)


def claims():
    return {
        "sessionID": "session-1",
        "correlationID": "tool-1",
        "rootOperationID": "root-1",
        "idempotencyKey": "idempotency-key-1",
    }


def envelope(**overrides):
    value = {
        "executorID": "openclank.search.v1",
        "operation": "web.search",
        "artifactIDs": [],
        "options": {
            "executorID": "openclank.search.v1",
            "contractVersion": 1,
            "action": "dispatch",
            "claims": claims(),
            "input": {"query": "open clank", "count": 5, "intent": "web", "extraction": "text", "deadlineAt": 1000},
        },
    }
    value.update(overrides)
    return value


def test_search_recipe_is_connected_to_outer_executor_schema():
    assert not list(VALIDATOR.iter_errors(envelope()))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["options"]["input"].update(intent="general"),
        lambda value: value["options"].update(handoffID="wrong-action"),
        lambda value: value["options"]["claims"].update(idempotencyKey="short"),
        lambda value: value["options"]["input"].update(accountID="forbidden"),
    ],
)
def test_invalid_search_payloads_are_rejected(mutate):
    value = envelope()
    mutate(value)
    assert list(VALIDATOR.iter_errors(value))


def response(**overrides):
    value = {
        "schema_version": 1, "query": "x", "caller": "chat", "mode": "results",
        "selected_provider": "brave", "actual_provider": "brave", "account_scope": "deployment-service",
        "account_id": "", "status": "complete", "sources": [{"url": "https://example.test/a", "title": "A", "snippet": "s"}],
        "total": 1, "cache": {"hit": False}, "elapsed_ms": 1, "warnings": [], "search_queries": [],
        "support_links": [], "web_search_usage": {},
    }
    value.update(overrides)
    return value


def test_artifact_result_schema_rejects_malformed_fields_and_scope():
    validator = Draft202012Validator(SCHEMA)
    artifact_schema = SCHEMA["$defs"]["searchArtifactResult"]
    def errors(value):
        return list(validator.descend(value, artifact_schema))
    assert not errors({"kind": "external_result", "response": response()})
    assert errors({"kind": "external_result", "response": response(total="1")})
    assert errors({"kind": "external_result", "response": response(account_scope="model-account")})


def test_actual_router_prepared_and_terminal_shapes_validate_canonically():
    validator = Draft202012Validator(SCHEMA)
    prepared = {
        "kind": "managed_request", "handoffID": "handoff-1", "request": {
            "rootOperationID": "root-1", "idempotencyKey": "idempotency-key-1", "operation": "web.search",
            "routes": [{"connectionID": "conn", "providerID": "mimo", "billingLane": "subscription", "modelRouteID": "route", "modelID": "mimo-search"}],
            "input": {"query": "open clank", "count": 10, "freshness": "week", "answer": True},
            "artifactInputs": [], "options": {"deadlineMs": 30000},
        },
    }
    terminal = {
        "kind": "finalized", "terminal": {
            "operationID": "operation-1", "rootOperationID": "root-1", "operation": "web.search",
            "modelRouteID": "route", "connectionID": "conn", "billingLane": "subscription", "state": "complete",
            "committed": True, "output": {"text": "ok"}, "artifacts": [], "replayed": False,
        },
    }
    assert not list(validator.descend(prepared, SCHEMA["$defs"]["searchArtifactResult"]))
    assert not list(validator.descend(terminal, SCHEMA["$defs"]["searchArtifactResult"]))

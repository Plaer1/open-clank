"""S03 typed facets use only a synthetic bridge; no app store is opened."""
import struct
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.copal_routes import setup_copal_routes


class FacetBridge:
    def __init__(self, root):
        self.data_dir = root
        self.calls = []
        self.docs = [
            {"id": "a", "head": "a1", "kind": "note", "name": "Notes/A", "text": "Alpha",
             "tags": ["top", "top"], "properties": {"mixed": [True, "true", "True", 1, 1.0, 1e-7, 1e20, " padded ", "", None, {}],
             "type": "not-kind", "tags": "not-tag", " exact ": "kept"}},
            {"id": "b", "head": "b1", "kind": "wiki", "name": "Notes/B", "text": "Beta",
             "tags": ["other"], "properties": {"mixed": [True, True, "true"], "unknownKey": "value"}},
        ]
        for document in self.docs:
            document.update(format="copal-note-v1", storage="database", frontmatter={}, links=[])

    def is_alive(self):
        return True

    async def call(self, operation, args, timeout=20):
        self.calls.append((operation, args))
        assert operation == "index"
        return {"docs": self.docs}


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(users={"alice": {}, "bob": {}}, account_id=lambda user: "fixture-" + user)
    @app.middleware("http")
    async def synthetic_session(request, call_next):
        request.state.current_user = request.headers.get("x-test-owner", "alice")
        request.state.authenticated = request.state.current_user in {"alice", "bob"}
        return await call_next(request)
    app.include_router(setup_copal_routes())
    app.state.copal_bridge = FacetBridge(tmp_path)
    return TestClient(app), app.state.copal_bridge


def test_typed_scalar_identity_counts_and_legacy_response(fixture):
    client, bridge = fixture
    body = client.get("/api/copal/graph/facets", params={"facet_version": 2}).json()
    assert body["version"] == 2 and body["countUnit"] == "documents"
    options = {item["id"]: item for item in body["facets"]["properties"]["mixed"]["values"]}
    assert options["boolean:true"]["value"] is True and options["boolean:true"]["count"] == 2
    assert options['string:"true"']["count"] == 2
    assert options['string:"True"']["count"] == 1
    assert options['string:" padded "']["label"] == '" padded "'
    for value in [1, 1e-7, 1e20]:
        assert options["number:" + struct.pack(">d", value).hex()]["value"] == value
    assert options["number:" + struct.pack(">d", 1).hex()]["count"] == 1
    assert len(options) == 7
    assert " exact " in body["propertyKeys"]
    assert body["facets"]["tags"]["values"][0]["count"] == 1
    legacy = client.get("/api/copal/graph/facets").json()
    assert legacy["version"] == 1
    assert all(isinstance(item["value"], str) for item in legacy["facets"]["properties"]["mixed"]["values"])
    assert "type" not in legacy["facets"]["properties"]["mixed"]["values"][0]
    assert "exact" in legacy["facets"]["properties"] and " exact " not in legacy["facets"]["properties"]
    assert all(operation == "index" for operation, _ in bridge.calls)


def test_exact_property_query_pages_and_generation(fixture):
    client, bridge = fixture
    params = {"facet_version": 2, "category": "properties", "property_key": "mixed", "limit": 2}
    first = client.get("/api/copal/graph/facets", params=params).json()
    assert set(first["facets"]["properties"]) == {"mixed"}
    assert first["catalog"]["kind"] == "query"
    assert first["facets"]["properties"]["mixed"]["hasMore"]
    second = client.get("/api/copal/graph/facets", params={**params, "offset": 2, "expected_generation": first["generation"]}).json()
    assert second["generation"] == first["generation"]
    assert first["facets"]["properties"]["mixed"]["values"] != second["facets"]["properties"]["mixed"]["values"]
    result = client.get("/api/copal/graph/facets", params={**params, "query": "padded"}).json()
    assert result["facets"]["properties"]["mixed"]["total"] == 1
    assert "unknownKey" in result["propertyKeys"]
    unknown = client.get("/api/copal/graph/facets", params={**params, "property_key": "absent"}).json()
    assert unknown["facets"]["properties"]["absent"]["total"] == 0
    bridge.docs[0]["head"] = "a2"
    assert client.get("/api/copal/graph/facets", params={**params, "expected_generation": first["generation"]}).status_code == 409


@pytest.mark.parametrize("params", [{"offset": -1}, {"limit": 0}, {"limit": 1001}, {"facet_version": 3},
    {"category": "tags", "property_key": "mixed"}, {"query": "a" * 257}])
def test_invalid_facet_bounds(fixture, params):
    assert fixture[0].get("/api/copal/graph/facets", params=params).status_code == 422


def test_scope_is_forwarded_and_generation_is_scope_bound(fixture, monkeypatch):
    client, bridge = fixture
    a = client.get("/api/copal/graph/facets?facet_version=2&workspace=one", headers={"x-test-owner": "alice"}).json()
    b = client.get("/api/copal/graph/facets?facet_version=2&workspace=one", headers={"x-test-owner": "bob"}).json()
    c = client.get("/api/copal/graph/facets?facet_version=2&workspace=two", headers={"x-test-owner": "alice"}).json()
    assert len({a["generation"], b["generation"], c["generation"]}) == 3
    assert [(args["owner"], args["workspace_id"]) for _, args in bridge.calls] == [("alice", "one"), ("bob", "one"), ("alice", "two")]
    assert client.get("/api/copal/graph/facets", headers={"x-test-owner": "unknown"}).status_code == 401

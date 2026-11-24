"""F10 related-memory: `graph` mode over typed edges with a traversal receipt; `semantic` mode unavailable (vectors are
disabled); never lexical similarity under either name. Guarded-v18 fixture over the real transport, plus route tests with fakes."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from src.memery_v2 import list_current_records
from routes.memory.native_ops_read import register_native_ops_read

_CACHE = {}


def fixture():
    """alice: two facts (share the `fact` category node), a goal whose text is very similar to a fact but has no edge to it; bob: one fact."""
    if "fx" not in _CACHE:
        fx = F.build_fixture()
        async def go():
            facts = await F.seed_records(fx, "alice", ["quarterly report draft one", "quarterly report draft two"])
            goal = await F.seed_records(fx, "alice", ["quarterly report draft one goal"], category="goal", tag="g")
            bob = await F.seed_records(fx, "bob", ["quarterly report draft one"], tag="bobbatch")
            return facts, goal, bob
        _CACHE["fx"] = fx
        _CACHE["rows"] = F.run(go())
    return _CACHE["fx"], _CACHE["rows"]


def test_graph_mode_returns_edge_related_record_with_type_and_complete_receipt():
    fx, (facts, goal, bob) = fixture()
    async def go():
        result = await fx.provider_for("alice").related_memories(facts[0]["curated_id"], owner="alice", mode="graph")
        assert result["mode"] == "graph" and result["state"] == "ok"
        assert [i["id"] for i in result["items"]] == [facts[1]["curated_id"]]
        item = result["items"][0]
        assert item["hops"] == 2 and item["text"] == "quarterly report draft two" and item["status"] == "active"
        assert [e["tag"] for e in item["edges"]] == ["is", "is"] and [e["direction"] for e in item["edges"]] == ["out", "in"]
        assert item["edges"][0]["via"] == {"kind": "category", "name": "fact"}
        assert result["receipt"] == {"complete": True, "limits": {"hops": 2, "nodes": 10, "edges": 25}, "truncated_by": None}
        # similar text without an edge (the goal) is not related; neither is another owner's record
        ids = {i["id"] for i in result["items"]}
        assert goal[0]["curated_id"] not in ids and bob[0]["curated_id"] not in ids
        # filters apply to the related records before the limit
        assert (await fx.provider_for("alice").related_memories(facts[0]["curated_id"], owner="alice", filters={"predicate": "goal"}))["items"] == []
        assert len((await fx.provider_for("alice").related_memories(facts[0]["curated_id"], owner="alice", filters={"predicate": "fact"}, limit=1))["items"]) == 1
        # the goal has no edge to any record
        assert (await fx.provider_for("alice").related_memories(goal[0]["curated_id"], owner="alice"))["items"] == []
    F.run(go())


def test_foreign_or_unknown_id_looks_like_no_related_items():
    fx, (facts, goal, bob) = fixture()
    async def go():
        own_none = await fx.provider_for("alice").related_memories(goal[0]["curated_id"], owner="alice")
        foreign = await fx.provider_for("alice").related_memories(bob[0]["curated_id"], owner="alice")
        unknown = await fx.provider_for("alice").related_memories("m_does_not_exist", owner="alice")
        assert foreign == unknown == own_none  # no existence oracle: identical empty, complete results
        assert foreign["items"] == [] and foreign["receipt"]["complete"] is True
        # bob asking about alice's record sees nothing either, and alice's records never appear in bob's results
        theirs = await fx.provider_for("bob").related_memories(facts[0]["curated_id"], owner="bob")
        assert theirs["items"] == []
    F.run(go())


def test_semantic_mode_is_unavailable_and_invalid_mode_refused():
    fx, (facts, _, _) = fixture()
    async def go():
        p = fx.provider_for("alice")
        assert await p.related_memories(facts[0]["curated_id"], owner="alice", mode="semantic") == \
            {"mode": "semantic", "state": "unavailable", "reason": "vectors_disabled", "items": []}
        for mode in ("lexical", "", "GRAPH", None):
            try:
                await p.related_memories(facts[0]["curated_id"], owner="alice", mode=mode)
            except Exception as exc:
                assert "unsupported" in str(exc)
                continue
            raise AssertionError(mode)
    F.run(go())


def test_truncated_traversal_is_reported_incomplete():
    fx, (facts, _, _) = fixture()
    async def go():
        p = fx.provider_for("alice")
        p._RELATED_EDGE_LIMIT = 1  # instance override: the `fact` category node has two edges
        try:
            result = await p.related_memories(facts[0]["curated_id"], owner="alice")
        finally:
            del p._RELATED_EDGE_LIMIT
        assert result["receipt"]["complete"] is False and result["receipt"]["truncated_by"] == "edges"
        assert result["receipt"]["limits"]["edges"] == 1
    F.run(go())


def test_forgotten_related_record_is_not_returned():
    fx = F.build_fixture()
    async def go():
        try:
            seeded = await F.seed_records(fx, "alice", ["alpha shared", "beta shared"])
            p = fx.provider_for("alice")
            # A related/graph read first (it bumps edge traversal_count), then the forget: must succeed (F-D3).
            before = await p.related_memories(seeded[0]["curated_id"], owner="alice")
            assert [i["id"] for i in before["items"]] == [seeded[1]["curated_id"]]
            owner, (account, ws) = "alice", fx.owners["alice"]
            refs = fx.coordinator.checked_refs(owner, account, ws)
            before_issue = lambda: fx.coordinator.checked_refs(owner, account, ws) == refs
            body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                    "action": "preview", "selector_kind": "record_id", "selector": seeded[1]["curated_id"],
                    "preview_token": None, "tombstone_id": None, "operation_id": None}
            preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
            await p._host_guarded_batch_forget({**body, "action": "commit", "preview_token": preview["token"], "operation_id": "1" * 32}, before_issue=before_issue)
            after = await p.related_memories(seeded[0]["curated_id"], owner="alice")
            assert after["items"] == []
        finally:
            await F.shutdown(fx)
    F.run(go())


def make_client(*, authed=True, live=True, guarded=True, provider=None):
    calls = []
    async def related(memory_id, owner=None, mode="graph", limit=10, filters=None):
        calls.append((memory_id, owner, mode, limit, filters))
        return {"mode": mode, "state": "ok", "items": [], "receipt": {"complete": True}}
    prov = provider if provider is not None else SimpleNamespace(_guarded_v17=guarded, related_memories=related)
    @asynccontextmanager
    async def admitted(user):
        yield
    ctx = {"owner": "owner-1", "barrier": SimpleNamespace(admitted=admitted), "current_live": lambda: live}
    def require_human(request):
        if not authed: raise HTTPException(401, "no")
        return "alice"
    async def owned(fn, *a, **k): return fn(*a, **k)
    router = APIRouter()
    register_native_ops_read(router, require_human=require_human, capture_context=lambda request, user: ctx, owned=owned, provider=prov)
    app = FastAPI(); app.include_router(router, prefix="/api/memory")
    return TestClient(app), calls


def test_route_passes_mode_limit_filters_and_owner_from_the_bracket():
    client, calls = make_client()
    r = client.get("/api/memory/m_1/related", params={"mode": "graph", "limit": "5", "kind": "fact"})
    assert r.status_code == 200 and r.json()["filters"] == {"kind": "fact"} and calls == [("m_1", "owner-1", "graph", 5, {"kind": "fact"})]
    assert client.get("/api/memory/m_1/related", params={"mode": "semantic"}).status_code == 200
    assert make_client(guarded=False)[0].get("/api/memory/m_1/related", params={"mode": "graph"}).status_code == 200


def test_route_refusals_never_reach_the_provider():
    for params in ({}, {"mode": "lexical"}, {"mode": "graph", "limit": "0"}, {"mode": "graph", "limit": "101"}, {"mode": "graph", "bogus": "1"},
                   {"mode": "graph", "status": "retired"}, {"mode": "graph", "owner": "bob"}, [("mode", "graph"), ("mode", "semantic")]):
        client, calls = make_client()
        r = client.get("/api/memory/m_1/related", params=params)
        assert r.status_code == 422 and calls == [], params
    client, calls = make_client(authed=False)
    assert client.get("/api/memory/m_1/related", params={"mode": "graph"}).status_code == 403 and calls == []
    client, calls = make_client(live=False)
    assert client.get("/api/memory/m_1/related", params={"mode": "graph"}).status_code == 503
    async def none(*a, **k): return None
    assert make_client(provider=SimpleNamespace(_guarded_v17=False, related_memories=none))[0].get("/api/memory/m_1/related", params={"mode": "graph"}).status_code == 503
    assert make_client(provider=SimpleNamespace(_guarded_v17=False))[0].get("/api/memory/m_1/related", params={"mode": "graph"}).status_code == 503

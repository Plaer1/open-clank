"""F7 entity/graph: scoped alias/type lookup and the traversal truncation receipt, through provider -> real memery-worker on the
guarded-v18 fixture, plus route tests with fakes. Records (and with them graph nodes, edges and v2 entities) are created
by the real explicit-add + create-only batch path, so the graph is genuine: each record is a `memory` node linked to a
`category` node; the v2 entity of each owner is labelled "memory" (same label for both owners on purpose)."""
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from tests.test_native_ops_filter_count import seed_main
from routes.memory.native_ops_read import register_native_ops_read

_CACHE = {}


def fixture():
    if "fx" not in _CACHE:
        fx = F.build_fixture(); seed_main(fx); _CACHE["fx"] = fx
        async def init():
            for provider in fx.providers.values():
                await provider.initialize()
        F.run(init())
    return _CACHE["fx"]


def entity_id(fx, owner):
    (row,) = F.rows(fx.db, "SELECT entity_id FROM fm_v2_entities WHERE owner_id=?", (owner,))
    return row[0]


def test_same_label_under_two_owners_is_never_merged():
    fx = fixture()
    async def go():
        a = await fx.provider_for("alice").entity_lookup_v2(owner="alice", alias="Memory")
        b = await fx.provider_for("bob").entity_lookup_v2(owner="bob", alias=" memory ")
        assert [e["entity_id"] for e in a["entities"]] == [entity_id(fx, "alice")]
        assert [e["entity_id"] for e in b["entities"]] == [entity_id(fx, "bob")]
        assert entity_id(fx, "alice") != entity_id(fx, "bob")
        assert a["entities"][0]["entity_type"] == "concept" and a["entities"][0]["matched"] == {"by": "canonical_label", "value": "memory"}
        assert a["receipt"]["complete"] is True and a["receipt"]["limits"] == {"hops": None, "nodes": 20, "edges": None}
        # type lookup is scoped the same way
        assert [e["entity_id"] for e in (await fx.provider_for("alice").entity_lookup_v2(owner="alice", entity_type="CONCEPT"))["entities"]] == [entity_id(fx, "alice")]
        # alice's provider (workspace ws-a) asked as owner bob: bob's entity lives in ws-b, so nothing
        assert (await fx.provider_for("alice").entity_lookup_v2(owner="bob", alias="memory"))["entities"] == []
        assert (await fx.provider_for("alice").entity_lookup_v2(owner="alice", alias="nothing-here"))["entities"] == []
    F.run(go())


def test_lookup_refuses_empty_or_oversized_requests_at_native():
    fx = fixture()
    async def go():
        provider = fx.provider_for("alice")
        for kw in ({}, {"alias": "  "}, {"alias": "memory", "limit": 0}, {"alias": "memory", "limit": 101}):
            try:
                await provider.entity_lookup_v2(owner="alice", **kw)
            except Exception:
                continue
            raise AssertionError(kw)
    F.run(go())


async def walk(provider, owner, ws, **args):
    return await provider._call_tool("graph_walk", {"owner": owner, "workspace_id": ws, **args})


def test_truncated_expand_reports_incomplete_and_full_walk_complete():
    fx = fixture()
    async def go():
        p = fx.provider_for("alice")
        cut = await walk(p, "alice", "ws-a", op="expand", node_kind="category", node_name="fact", limit=1)
        assert len(cut["hits"]) == 1
        assert cut["receipt"] == {"complete": False, "limits": {"hops": 1, "nodes": None, "edges": 1}, "truncated_by": "edges"}
        full = await walk(p, "alice", "ws-a", op="expand", node_kind="category", node_name="fact", limit=5)
        assert len(full["hits"]) == 2 and full["receipt"]["complete"] is True and full["receipt"]["truncated_by"] is None
        assert {h["other"]["kind"] for h in full["hits"]} == {"memory"}
        # another owner's node id is unreadable from this scope: empty and complete, same as an unknown id
        bob_node = (await walk(fx.provider_for("bob"), "bob", "ws-b", op="expand", node_kind="category", node_name="fact", limit=5))["node_id"]
        foreign = await walk(p, "alice", "ws-a", op="expand", node_id=bob_node, limit=5)
        unknown = await walk(p, "alice", "ws-a", op="expand", node_id="no-such-node", limit=5)
        assert foreign["hits"] == [] and foreign["receipt"] == unknown["receipt"] and foreign["receipt"]["complete"] is True
        # explicit kind/name and node_id are exclusive
        try:
            await walk(p, "alice", "ws-a", op="expand", node_id="x", node_kind="memory", node_name="y")
        except Exception:
            pass
        else:
            raise AssertionError("both node selectors accepted")
    F.run(go())


def test_trace_beyond_hop_limit_is_incomplete_not_a_no_path_proof():
    fx = fixture()
    async def go():
        p = fx.provider_for("alice")
        # alice fact one and fact two are joined only through the `fact` category node: two hops apart
        first = await walk(p, "alice", "ws-a", op="expand", node_kind="category", node_name="fact", limit=5)
        a, b = [h["other"]["id"] for h in first["hits"]]
        short = await walk(p, "alice", "ws-a", op="trace", node_id=a, dst_id=b, max_depth=1, limit=3)
        assert short["paths"] == [] and short["receipt"]["complete"] is False and short["receipt"]["truncated_by"] == "hops"
        assert short["receipt"]["limits"] == {"hops": 1, "nodes": None, "edges": None}
        long = await walk(p, "alice", "ws-a", op="trace", node_id=a, dst_id=b, max_depth=3, limit=3)
        assert len(long["paths"]) == 1 and len(long["paths"][0]["node_ids"]) == 3 and long["receipt"]["complete"] is True
        # frontier mode with a small node limit
        frontier = await walk(p, "alice", "ws-a", op="trace", node_id=a, max_depth=3, limit=1)
        assert len(frontier["paths"]) == 1 and frontier["receipt"]["truncated_by"] == "nodes"
    F.run(go())


def make_client(*, authed=True, live=True, guarded=True, provider=None):
    calls = []
    async def lookup(owner=None, alias=None, entity_type=None, limit=20):
        calls.append((owner, alias, entity_type, limit))
        return {"entities": [{"entity_id": "e1"}], "receipt": {"complete": True}}
    prov = provider if provider is not None else SimpleNamespace(_guarded_v17=guarded, entity_lookup_v2=lookup)
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


def test_route_returns_entities_with_owner_from_the_bracket():
    client, calls = make_client()
    r = client.get("/api/memory/entities", params={"alias": "memory", "type": "concept", "limit": "5"})
    assert r.status_code == 200 and r.json() == {"entities": [{"entity_id": "e1"}], "receipt": {"complete": True}}
    assert calls == [("owner-1", "memory", "concept", 5)] and r.headers["cache-control"] == "private, no-store"
    assert make_client(guarded=False)[0].get("/api/memory/entities", params={"type": "x"}).status_code == 200


def test_route_refusals_never_reach_the_provider():
    for params in ({}, {"limit": "5"}, {"alias": " "}, {"alias": "x" * 513}, {"alias": "a", "limit": "0"}, {"alias": "a", "limit": "101"},
                   {"alias": "a", "limit": "x"}, {"alias": "a", "owner": "bob"}, {"alias": "a", "workspace_id": "w"}, [("alias", "a"), ("alias", "b")]):
        client, calls = make_client()
        r = client.get("/api/memory/entities", params=params)
        assert r.status_code == 422 and calls == [], params
    client, calls = make_client(authed=False)
    assert client.get("/api/memory/entities", params={"alias": "a"}).status_code == 403 and calls == []
    client, calls = make_client(live=False)
    r = client.get("/api/memory/entities", params={"alias": "a"})
    assert r.status_code == 503 and "entities" not in r.text
    assert make_client(provider=SimpleNamespace(_guarded_v17=False))[0].get("/api/memory/entities", params={"alias": "a"}).status_code == 503

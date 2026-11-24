"""F5 lexical search + component receipt (src/memery_v2.search_current_records) on the guarded-v18 fixture, and the route.

Contract row "Text/vector search": scoped exact result survives unavailable vectors. Vectors are reported unavailable
(DisabledEmbeddingClient, memery-core embed.rs). Records are created through the real explicit-add + create-only batch path.
"""
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from tests.test_native_ops_filter_count import seed_main
from src.memery_v2 import search_current_records
from routes.memory.native_ops_read import register_native_ops_read

# (result count, sha256 of json.dumps(rows, sort_keys=True, default=repr)) per "owner|workspace|query|top_k",
# captured from the UNMODIFIED search_current_records on exactly the data seed_main() creates.
UNCHANGED = {
    "alice|ws-a|alice|5": (3, "90cb051ea32fc7bbe474966acc7fd571ee772594f827f158ccfd07346d015109"),
    "alice|ws-a|alice|1": (1, "35f345181671f1f72dcb614b30f90e1f4d7d02b9a29c19b8405957c803ce824a"),
    "alice|ws-a|fact|5": (3, "90cb051ea32fc7bbe474966acc7fd571ee772594f827f158ccfd07346d015109"),
    "alice|ws-a|fact|1": (1, "35f345181671f1f72dcb614b30f90e1f4d7d02b9a29c19b8405957c803ce824a"),
    "alice|ws-a|alice goal|5": (3, "19a0960ed126f2ff47b095fa6296aacc0f3052e59133ccb2c3f37a862b0b3230"),
    "alice|ws-a|alice goal|1": (1, "ed734897d3d4bf51d37806423d58329019c12365d837057ab5ab366c3aaefda3"),
    "alice|ws-a|one two|5": (2, "61995eed2f987c3be7aeee1e7b0aee30f69227b13de423344ef5e008726c419b"),
    "alice|ws-a|one two|1": (1, "534569e7bc11a6072b18d8af2beb83293ae45df30c5a1cfa21527c9f953af0e6"),
    "alice|ws-a|nothing here|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-a|nothing here|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-a|bob fact|5": (3, "eb3b78086a83e5ffb8820ba2a9e96f7667b83e9ad7e41854c5678e2bf42994d7"),
    "alice|ws-a|bob fact|1": (1, "534569e7bc11a6072b18d8af2beb83293ae45df30c5a1cfa21527c9f953af0e6"),
    "bob|ws-b|alice|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|alice|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|fact|5": (1, "a477376d6d6a852395476d2beb108e313071c6f9f42d6dca32f92d111a6b6b5f"),
    "bob|ws-b|fact|1": (1, "a477376d6d6a852395476d2beb108e313071c6f9f42d6dca32f92d111a6b6b5f"),
    "bob|ws-b|alice goal|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|alice goal|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|one two|5": (1, "1dfcb9224b6f6bd687b31696923401a22c957a26e964a57b1bbe85bd1f3a3559"),
    "bob|ws-b|one two|1": (1, "1dfcb9224b6f6bd687b31696923401a22c957a26e964a57b1bbe85bd1f3a3559"),
    "bob|ws-b|nothing here|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|nothing here|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "bob|ws-b|bob fact|5": (1, "a477376d6d6a852395476d2beb108e313071c6f9f42d6dca32f92d111a6b6b5f"),
    "bob|ws-b|bob fact|1": (1, "a477376d6d6a852395476d2beb108e313071c6f9f42d6dca32f92d111a6b6b5f"),
    "alice|ws-b|alice|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|alice|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|fact|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|fact|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|alice goal|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|alice goal|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|one two|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|one two|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|nothing here|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|nothing here|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|bob fact|5": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    "alice|ws-b|bob fact|1": (0, "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
}
_CACHE = {}


def fixture():
    if "fx" not in _CACHE:
        fx = F.build_fixture(); seed_main(fx); _CACHE["fx"] = fx
    return _CACHE["fx"]


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=repr).encode()).hexdigest()


def search(fx, query, owner, ws, **kw):
    return search_current_records(query, owner=owner, workspace_id=ws, db_path=str(fx.db), **kw)


def test_default_output_is_identical_to_unmodified_function():
    fx = fixture()
    for key, (n, expected) in UNCHANGED.items():
        owner, ws, query, k = key.split("|")
        rows = search(fx, query, owner, ws, top_k=int(k))
        assert len(rows) == n and digest(rows) == expected, key
        # with_receipt=True returns the very same rows
        rows2, receipt = search(fx, query, owner, ws, top_k=int(k), with_receipt=True)
        assert digest(rows2) == expected, key


def test_receipt_shape_is_closed_and_vectors_are_unavailable():
    fx = fixture()
    rows, receipt = search(fx, "alice", "alice", "ws-a", top_k=2, with_receipt=True)
    assert receipt == {"version": 1,
        "lexical": {"state": "ok", "candidates": 3, "returned": 2,
                    "rank_basis": "query_token_coverage_over_text_kind_predicate_tags_plus_phrase_bonus"},
        "vector": {"state": "unavailable", "reason": "vectors_disabled"}, "merge": "lexical_only", "tie_break": "stable_id"}
    assert len(rows) == 2
    # no match and empty query: still a receipt, lexical ok with zero candidates, vector unavailable
    for query in ("nothing here", "  ", "a"):
        rows, receipt = search(fx, query, "alice", "ws-a", with_receipt=True)
        assert rows == [] and receipt["lexical"]["candidates"] == 0 and receipt["lexical"]["returned"] == 0
        assert receipt["vector"] == {"state": "unavailable", "reason": "vectors_disabled"}


def test_equal_scores_are_ordered_by_stable_id_not_by_row_order():
    fx = fixture()
    rows = search(fx, "fact", "alice", "ws-a")
    ids = [r["id"] for r in rows]
    assert len(ids) == 3 and ids == sorted(ids)  # all three tie on "fact": order is id order
    # the unfiltered list order (created_at desc) differs, so this is the sort, not the source order
    from src.memery_v2 import list_current_records
    assert [r["id"] for r in list_current_records(owner="alice", workspace_id="ws-a", db_path=str(fx.db))] != ids


def test_foreign_owner_cannot_appear_or_change_candidates_and_returned():
    fx = fixture()
    # bob's record matches "bob fact one" better than anything alice owns; alice never sees it
    rows, receipt = search(fx, "bob fact one", "alice", "ws-a", with_receipt=True)
    assert all(r["owner"] == "alice" and "bob" not in r["text"] for r in rows)
    assert receipt["lexical"]["candidates"] == 3  # alice's own three records match "fact"; bob's is not counted
    rows_b, receipt_b = search(fx, "bob fact one", "bob", "ws-b", with_receipt=True)
    assert [r["text"] for r in rows_b] == ["bob fact one"] and receipt_b["lexical"] == {**receipt_b["lexical"], "candidates": 1, "returned": 1}
    # wrong workspace for the right owner, and a query only the foreign owner can match
    assert search(fx, "bob", "alice", "ws-a") == [] and search(fx, "alice", "bob", "ws-b") == []
    assert search(fx, "fact", "alice", "ws-b", with_receipt=True)[1]["lexical"]["candidates"] == 0
    # limit=1: the foreign better match cannot displace alice's best result
    top = search(fx, "bob fact one", "alice", "ws-a", top_k=1)
    assert len(top) == 1 and top[0]["owner"] == "alice"


def test_filters_narrow_before_scoring():
    fx = fixture()
    rows, receipt = search(fx, "alice", "alice", "ws-a", filters={"predicate": "goal"}, with_receipt=True)
    assert [r["text"] for r in rows] == ["alice goal"] and receipt["lexical"]["candidates"] == 1
    for bad in ({"x": "y"}, {"status": "retired"}):
        try:
            search(fx, "alice", "alice", "ws-a", filters=bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_forgotten_record_is_not_returned():
    fx = F.build_fixture()
    async def go():
        try:
            seeded = await F.seed_records(fx, "alice", ["keepword stays", "forgetword goes"])
            owner, (account, ws) = "alice", fx.owners["alice"]
            provider, coordinator = fx.provider_for("alice"), fx.coordinator
            refs = coordinator.checked_refs(owner, account, ws)
            before_issue = lambda: coordinator.checked_refs(owner, account, ws) == refs
            body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                    "action": "preview", "selector_kind": "record_id", "selector": seeded[1]["curated_id"],
                    "preview_token": None, "tombstone_id": None, "operation_id": None}
            preview = await provider._host_guarded_batch_forget(body, before_issue=before_issue)
            await provider._host_guarded_batch_forget({**body, "action": "commit", "preview_token": preview["token"], "operation_id": "1" * 32}, before_issue=before_issue)
        finally:
            await F.shutdown(fx)
    F.run(go())
    assert [r["text"] for r in search(fx, "forgetword", "alice", "ws-a")] == []
    rows, receipt = search(fx, "keepword", "alice", "ws-a", with_receipt=True)
    assert [r["text"] for r in rows] == ["keepword stays"] and receipt["lexical"]["candidates"] == 1


def make_client(*, authed=True, live=True, guarded=True, provider=None):
    calls = []
    async def searcher(query, owner=None, top_k=10, filters=None):
        calls.append((query, owner, top_k, filters))
        row = {"id": "m1", "text": "t", "kind": "fact", "status": "active", "owner": owner, "tags": ["x"], "recall_score": 1.0,
               "metadata": {"predicate": "fact"}, "_revision_ref": object()}
        return [row, {**row, "id": "foreign", "owner": "someone-else"}], {"version": 1}
    prov = provider if provider is not None else SimpleNamespace(_guarded_v17=guarded, search_v2_with_receipt=searcher)
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


def test_route_returns_items_and_receipt_and_defends_owner_scope():
    client, calls = make_client()
    r = client.get("/api/memory/search-receipt", params={"q": "hello", "limit": "5", "kind": "fact"})
    assert r.status_code == 200 and calls == [("hello", "owner-1", 5, {"kind": "fact"})]
    body = r.json()
    assert body["receipt"] == {"version": 1} and body["filters"] == {"kind": "fact"}
    assert body["items"] == [{"id": "m1", "text": "t", "kind": "fact", "status": "active", "predicate": "fact", "tags": ["x"], "score": 1.0}]
    assert r.headers["cache-control"] == "private, no-store"
    assert make_client(guarded=False)[0].get("/api/memory/search-receipt", params={"q": "x"}).status_code == 200


def test_route_refusals_never_reach_the_provider():
    for params in ({}, {"q": ""}, {"q": "  "}, {"q": "x" * 513}, {"q": "x", "limit": "0"}, {"q": "x", "limit": "101"},
                   {"q": "x", "limit": "-1"}, {"q": "x", "limit": "a"}, {"q": "x", "bogus": "1"}, {"q": "x", "status": "retired"},
                   [("q", "a"), ("q", "b")]):
        client, calls = make_client()
        r = client.get("/api/memory/search-receipt", params=params)
        assert r.status_code == 422 and calls == [], params
    client, calls = make_client(authed=False)
    assert client.get("/api/memory/search-receipt", params={"q": "x"}).status_code == 403 and calls == []
    client, calls = make_client(live=False)
    r = client.get("/api/memory/search-receipt", params={"q": "x"})
    assert r.status_code == 503 and "items" not in r.text
    async def none(*a, **k): return None
    assert make_client(provider=SimpleNamespace(_guarded_v17=False, search_v2_with_receipt=none))[0].get("/api/memory/search-receipt", params={"q": "x"}).status_code == 503
    assert make_client(provider=SimpleNamespace(_guarded_v17=False))[0].get("/api/memory/search-receipt", params={"q": "x"}).status_code == 503

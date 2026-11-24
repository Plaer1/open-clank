"""F4 scoped filter + exact count (Host side, src/memery_v2.py) on the guarded-v18 fixture, plus route tests with fakes.

Records are created through the real explicit-add + create-only batch path (tests/_native_ops_fixture.py), so the list
and the count read genuine rows. No model, no network, no UI.
"""
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from src.memery_v2 import count_current_records, list_current_records
from routes.memory.native_ops_read import register_native_ops_read

# Digests of json.dumps(list_current_records(...), sort_keys=True), captured from the UNMODIFIED function (before
# filters existed) on exactly the data seeded by seed_main(): ids are content-derived, so they are stable across stores.
IDENTITY = {
    ("alice", "ws-a", None): ("984a21ab847a20cbe1b80b9ccd3968efdd1e32a68b308d98a54a240fc01a381d", 3),
    ("alice", "ws-b", None): ("4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945", 0),
    ("bob", "ws-b", None): ("4937460bfd0aaef98f3c103f04aa4427a1fb757c2cae5c8da62669cfe7accc77", 1),
    ("alice", "ws-a", 1): ("20f8ebed52e3be6711daa9f7bf13536b27d19fba6d9bf2d36757d3a2fc3cff0d", 1),
}
_CACHE = {}


def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def seed_main(fx):
    async def go():
        await F.seed_records(fx, "alice", ["alice fact one", "alice fact two"])
        await F.seed_records(fx, "alice", ["alice goal"], category="goal", tag="g")
        await F.seed_records(fx, "bob", ["bob fact one"])
    F.run(go())


def main_fixture():
    if "fx" not in _CACHE:
        fx = F.build_fixture()
        seed_main(fx)
        _CACHE["fx"] = fx
    return _CACHE["fx"]


def lst(fx, owner, ws, **kw):
    return list_current_records(owner=owner, workspace_id=ws, db_path=str(fx.db), **kw)


def cnt(fx, owner, ws, **kw):
    return count_current_records(owner=owner, workspace_id=ws, db_path=str(fx.db), **kw)


def test_unfiltered_list_is_identical_to_unmodified_function():
    fx = main_fixture()
    for (owner, ws, limit), (expected, n) in IDENTITY.items():
        rows = lst(fx, owner, ws) if limit is None else lst(fx, owner, ws, limit=limit)
        assert len(rows) == n and digest(rows) == expected, (owner, ws, limit)
        # filters=None and an empty dict are the same read
        again = lst(fx, owner, ws, filters=None) if limit is None else lst(fx, owner, ws, limit=limit, filters=None)
        assert digest(again) == expected


def entity_of(fx, owner):
    found = {r[0] for r in F.rows(fx.db, "SELECT subject_entity_id FROM fm_v2_knowledge_blocks WHERE owner_id=?", (owner,))}
    assert len(found) == 1
    return found.pop()


def created(fx, owner):
    return {r[0]: r[1] for r in F.rows(
        fx.db, "SELECT b.block_id,r.created_at FROM fm_v2_knowledge_blocks b JOIN fm_v2_knowledge_revisions r "
               "ON r.owner_id=b.owner_id AND r.block_id=b.block_id AND r.revision=b.current_revision WHERE b.owner_id=?", (owner,))}


def test_count_equals_list_total_for_no_filter_and_each_filter():
    fx = main_fixture()
    when = created(fx, "alice")
    newest = max(when.values())
    cases = [
        (None, 3), ({}, 3), ({"kind": "fact"}, 3), ({"kind": "goal"}, 0), ({"kind": "open_question"}, 0),
        ({"status": "active"}, 3), ({"status": "open"}, 0),
        ({"predicate": "goal"}, 1), ({"predicate": "fact"}, 2), ({"predicate": "nothing"}, 0),
        ({"entity": entity_of(fx, "alice")}, 3), ({"entity": "entity_nope"}, 0),
        ({"created_after": "2000-01-01T00:00:00+00:00"}, 3), ({"created_after": "2999-01-01"}, 0),
        ({"created_before": "2999-01-01T00:00:00Z"}, 3), ({"created_before": "2000-01-01"}, 0),
        ({"created_after": newest}, 1), ({"created_before": newest}, 2),
        ({"kind": "fact", "predicate": "fact", "created_before": newest}, 2),
    ]
    for filters, expected in cases:
        listed = lst(fx, "alice", "ws-a", limit=None, filters=filters)
        assert len(listed) == expected, (filters, len(listed))
        assert cnt(fx, "alice", "ws-a", filters=filters) == expected, filters
        # the count is also the filtered view of the unfiltered list (no second reading of the rules)
    full = lst(fx, "alice", "ws-a", limit=None)
    assert {r["id"] for r in lst(fx, "alice", "ws-a", limit=None, filters={"predicate": "goal"})} == \
        {r["id"] for r in full if r["metadata"]["predicate"] == "goal"}


def test_two_owners_cannot_displace_leak_or_inflate():
    fx = main_fixture()
    # limit=1: each owner gets its own newest record, never the other's
    a1 = lst(fx, "alice", "ws-a", limit=1)
    b1 = lst(fx, "bob", "ws-b", limit=1)
    assert [r["text"] for r in a1] == ["alice goal"] and [r["text"] for r in b1] == ["bob fact one"]
    assert [r["owner"] for r in a1] == ["alice"] and [r["owner"] for r in b1] == ["bob"]
    assert [r["text"] for r in lst(fx, "alice", "ws-a", limit=1, filters={"kind": "fact"})] == ["alice goal"]
    # counts are independent and scope-bound: wrong owner/workspace combinations see nothing
    assert (cnt(fx, "alice", "ws-a"), cnt(fx, "bob", "ws-b")) == (3, 1)
    assert cnt(fx, "alice", "ws-b") == 0 and cnt(fx, "bob", "ws-a") == 0
    assert cnt(fx, "alice", "ws-a", filters={"predicate": "goal"}) == 1 and cnt(fx, "bob", "ws-b", filters={"predicate": "goal"}) == 0
    # a filter value taken from the other owner cannot widen the scope
    assert cnt(fx, "alice", "ws-a", filters={"entity": entity_of(fx, "bob")}) in (0, 3)
    if entity_of(fx, "bob") != entity_of(fx, "alice"):
        assert cnt(fx, "alice", "ws-a", filters={"entity": entity_of(fx, "bob")}) == 0
    assert all(r["owner"] == "bob" for r in lst(fx, "bob", "ws-b", limit=None))


def test_provider_wrapper_counts_inside_its_own_scope():
    fx = main_fixture()
    async def go():
        a, b = fx.provider_for("alice"), fx.provider_for("bob")
        assert await a.count_v2_records(owner="alice") == 3
        assert await a.count_v2_records(owner="alice", filters={"predicate": "goal"}) == 1
        assert await b.count_v2_records(owner="bob") == 1
        assert await a.count_v2_records(owner="bob") == 0  # alice's workspace holds no record of bob
    F.run(go())


def test_forgotten_record_is_neither_listed_nor_counted():
    fx = F.build_fixture()
    async def go():
        try:
            seeded = await F.seed_records(fx, "alice", ["keep me", "forget me"])
            await F.seed_records(fx, "bob", ["bob keeps"])
            assert cnt(fx, "alice", "ws-a") == 2
            owner, (account, ws) = "alice", fx.owners["alice"]
            provider, coordinator = fx.provider_for("alice"), fx.coordinator
            refs = coordinator.checked_refs(owner, account, ws)
            before_issue = lambda: coordinator.checked_refs(owner, account, ws) == refs
            target = seeded[1]
            body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                    "action": "preview", "selector_kind": "record_id", "selector": target["curated_id"],
                    "preview_token": None, "tombstone_id": None, "operation_id": None}
            preview = await provider._host_guarded_batch_forget(body, before_issue=before_issue)
            commit = {**body, "action": "commit", "preview_token": preview["token"], "operation_id": "1" * 32}
            result = await provider._host_guarded_batch_forget(commit, before_issue=before_issue)
            assert result.get("tombstone_id")
        finally:
            await F.shutdown(fx)
    F.run(go())
    left = [r["text"] for r in lst(fx, "alice", "ws-a", limit=None)]
    assert left == ["keep me"], left
    assert cnt(fx, "alice", "ws-a") == 1 and cnt(fx, "alice", "ws-a", filters={"kind": "fact"}) == 1
    assert cnt(fx, "bob", "ws-b") == 1


def test_invalid_filters_are_refused_before_any_query():
    bad = [{"nope": "x"}, {"kind": ""}, {"kind": 1}, {"kind": " x"}, {"kind": "a\nb"}, {"status": "retired"}, {"status": None},
           {"created_after": "yesterday"}, {"created_before": 5}, {"created_after": "x" * 100}, "kind=fact", ["kind"],
           {"kind": "x" * 513}, {str(i): "v" for i in range(9)}]
    for filters in bad:
        for fn in (count_current_records, list_current_records):
            try:
                # a database path that cannot exist proves the refusal comes before any query
                fn(owner="alice", workspace_id="ws-a", filters=filters, db_path="/nonexistent-dir/none.sqlite")
            except ValueError:
                continue
            raise AssertionError(("accepted", filters))
    # a read failure raises instead of counting zero
    try:
        count_current_records(owner="alice", workspace_id="ws-a", db_path="/nonexistent-dir/none.sqlite")
    except Exception as exc:
        assert not isinstance(exc, ValueError)
    else:
        raise AssertionError("count swallowed a read error")


def make_client(*, authed=True, live=True, guarded=True, provider=None, total=7):
    calls = []
    async def counter(owner, filters=None):
        calls.append((owner, filters)); return total
    prov = provider if provider is not None else SimpleNamespace(_guarded_v17=guarded, count_v2_records=counter)
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


def test_route_returns_count_and_validated_filters():
    client, calls = make_client()
    r = client.get("/api/memory/count")
    assert r.status_code == 200 and r.json() == {"eligible_count": 7, "filters": {}} and calls == [("owner-1", None)]
    r = client.get("/api/memory/count", params={"kind": "fact", "created_after": "2026-01-01"})
    assert r.json()["filters"] == {"kind": "fact", "created_after": "2026-01-01"} and calls[-1] == ("owner-1", {"kind": "fact", "created_after": "2026-01-01"})
    assert r.headers["cache-control"] == "private, no-store"
    legacy, calls = make_client(guarded=False)  # legacy profile: plain owner scoping, no paired bracket
    assert legacy.get("/api/memory/count").json()["eligible_count"] == 7


def test_route_refusals_never_reach_the_provider():
    for params in ({"bogus": "1"}, {"kind": ""}, {"status": "retired"}, {"created_after": "soon"}, [("kind", "a"), ("kind", "b")]):
        client, calls = make_client()
        r = client.get("/api/memory/count", params=params)
        assert r.status_code == 422 and r.json() == {"state": "invalid_filter"} and calls == [], params
    client, calls = make_client(authed=False)
    assert client.get("/api/memory/count").status_code == 403 and calls == []
    client, calls = make_client(live=False)
    r = client.get("/api/memory/count")
    assert r.status_code == 503 and r.json() == {"state": "unavailable"} and "eligible_count" not in r.text
    client, _ = make_client(provider=SimpleNamespace(_guarded_v17=False))  # provider without the reader
    assert client.get("/api/memory/count").status_code == 503
    async def none(owner, filters=None): return None
    client, _ = make_client(provider=SimpleNamespace(_guarded_v17=False, count_v2_records=none))  # no local store
    assert client.get("/api/memory/count").status_code == 503

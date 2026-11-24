"""Diff and blame reads (routes/memory/native_ops_supersede.py + provider versioned_diff / versioned_blame) on the guarded-v18
fixture over the real store, plus route tests with fakes.

The second revision of a record is made through the real prepared-edit (value successor) path, the only edit path that
works on explicit-add records (the plain mirror path refuses them; see C4-F3 note). No model, no network, no UI.
"""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from src.memery_provider import _HostMirrorContext
from src.memory_versioned import VersionedMemoryNotFound
from routes.memory.native_ops_supersede import register_native_ops_supersede, FIELD_BYTES

_CACHE = {}


def world():
    if "w" in _CACHE:
        return _CACHE["w"]
    fx = F.build_fixture()
    async def go():
        a = (await F.seed_records(fx, "alice", ["alice original"]))[0]
        await F.seed_records(fx, "bob", ["bob record"])
        p = fx.providers["alice"]; acct, ws = fx.owners["alice"]
        ctx = await p._host_mirror_context(_HostMirrorContext("alice", ws, a["curated_id"], account_id=acct))
        refs = fx.coordinator.checked_refs("alice", acct, ws)
        dl = lambda: asyncio.get_running_loop().time() + 5
        req = {"version": 1, "owner": "alice", "workspace_id": ws, "account_id": acct, "curated_id": a["curated_id"],
               "predecessor_receipt_id": ctx["prior_receipt_id"], "expected_revision": ctx["expected_revision"],
               "reason": "edit", "value": "alice edited", "policy_projection": refs}
        preview = await p._host_value_successor_preview(owner="alice", workspace_id=ws, request=req, deadline=dl())
        applied = await p._host_value_successor_apply(owner="alice", workspace_id=ws, request=preview["request"], deadline=dl())
        assert applied["state"] == "applied" and applied["revision"] == 2, applied
        bob = F.rows(fx.db, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE owner_id='bob'")[0][0]
        return a, bob
    a, bob = F.run(go())
    _CACHE["w"] = (fx, a, bob)
    return _CACHE["w"]


def call(fx, owner, name, *args, **kw):
    return F.run(getattr(fx.providers[owner], name)(*args, owner=owner, **kw))


def test_diff_shows_exactly_the_changed_fields():
    fx, a, _ = world()
    d = call(fx, "alice", "versioned_diff", a["curated_id"], from_revision=1, to_revision=2)
    assert d["from_revision"] == 1 and d["to_revision"] == 2
    assert d["changes"]["value"]["before"]["value"] == "alice original" and d["changes"]["value"]["after"]["value"] == "alice edited", d["changes"]
    # every other reported field really differs between the two stored revisions, and nothing else differs
    h = call(fx, "alice", "versioned_detail", a["curated_id"])["history"]
    r1, r2 = [x for x in h if x["revision"] == 1][0], [x for x in h if x["revision"] == 2][0]
    ignored = {"text", "revision", "content_hash", "actor_type", "actor_id", "reason", "change_set_id", "previous_revision", "created_at"}
    expected = {k for k in set(r1) | set(r2) if k not in ignored and r1.get(k) != r2.get(k)}
    assert set(d["changes"]) == expected and "value" in expected, (set(d["changes"]), expected)
    same = call(fx, "alice", "versioned_diff", a["curated_id"], from_revision=2, to_revision=2)
    assert same["changes"] == {}


def test_blame_attributes_each_field_to_its_revision():
    fx, a, _ = world()
    b = call(fx, "alice", "versioned_blame", a["curated_id"])
    d = call(fx, "alice", "versioned_diff", a["curated_id"], from_revision=1, to_revision=2)
    assert b["fields"]["value"]["revision"] == 2
    for field, entry in b["fields"].items():
        assert entry["revision"] == (2 if field in d["changes"] else 1), (field, entry)
        assert set(entry) == {"revision", "actor_type", "actor_id", "reason", "created_at"}
    assert b["fields"]["value"]["actor_id"] == "alice"


def test_foreign_unknown_and_wrong_workspace_ids_are_indistinguishable():
    fx, a, bob = world()
    outcomes = set()
    cases = [("bob", a["curated_id"]), ("bob", a["block_id"]), ("alice", bob), ("alice", "no-such-id"), ("alice", " ")]
    for owner, ident in cases:
        for name, kw in (("versioned_diff", {"from_revision": 1, "to_revision": 1}), ("versioned_blame", {})):
            try:
                call(fx, owner, name, ident, **kw)
            except VersionedMemoryNotFound as exc:
                outcomes.add(str(exc) if ident.strip() else "x")
                outcomes.discard("x")
            else:
                raise AssertionError((owner, ident, name))
    assert len(outcomes) == 1, outcomes
    # the right id in the wrong workspace: alice's record read through a provider bound to ws-b, with alice as owner
    wrong = fx.providers["bob"]
    try:
        F.run(wrong.versioned_blame(a["curated_id"], owner="alice"))
    except VersionedMemoryNotFound:
        pass
    else:
        raise AssertionError("wrong workspace read succeeded")


def test_missing_revisions_on_an_owned_record_are_refused():
    from src.memery_v2 import V2OperationError
    fx, a, _ = world()
    for pair in ((1, 3), (3, 1), (99, 99)):
        try:
            call(fx, "alice", "versioned_diff", a["curated_id"], from_revision=pair[0], to_revision=pair[1])
        except V2OperationError as exc:
            assert exc.code == "not_found"
        else:
            raise AssertionError(pair)


def make_client(*, authed=True, live=True, guarded=True, provider=None):
    calls = []
    async def diff(memory_id, owner, from_revision, to_revision):
        calls.append(("diff", memory_id, owner, from_revision, to_revision))
        if memory_id == "gone": raise VersionedMemoryNotFound("knowledge was not found in this scope")
        return {"block_id": "b1", "from_revision": from_revision, "to_revision": to_revision,
                "changes": {"value": {"before": "x", "after": "y" * (FIELD_BYTES + 1)}, "tags": {"before": [], "after": ["t"]}}}
    async def blame(memory_id, owner):
        calls.append(("blame", memory_id, owner))
        if memory_id == "gone": raise VersionedMemoryNotFound("knowledge was not found in this scope")
        return {"block_id": "b1", "fields": {"value": {"revision": 2}}}
    prov = provider if provider is not None else SimpleNamespace(_guarded_v17=guarded, versioned_diff=diff, versioned_blame=blame)
    @asynccontextmanager
    async def admitted(user):
        yield
    ctx = {"owner": "owner-1", "barrier": SimpleNamespace(admitted=admitted), "current_live": lambda: live}
    def require_human(request):
        if not authed: raise HTTPException(401, "no")
        return "alice"
    async def owned(fn, *a, **k): return fn(*a, **k)
    router = APIRouter()
    register_native_ops_supersede(router, require_human=require_human, capture_context=lambda request, user: ctx, owned=owned, provider=prov)
    app = FastAPI(); app.include_router(router, prefix="/api/memory")
    return TestClient(app), calls


def test_route_returns_bounded_diff_and_blame():
    client, calls = make_client()
    r = client.get("/api/memory/m1/diff", params={"from": 1, "to": 2})
    assert r.status_code == 200 and calls == [("diff", "m1", "owner-1", 1, 2)] and r.headers["cache-control"] == "private, no-store"
    body = r.json()
    assert body["changes"]["tags"] == {"before": [], "after": ["t"]}
    assert body["changes"]["value"]["after"]["truncated"] is True and body["changes"]["value"]["before"] == "x" and len(r.text) < 2000
    r = client.get("/api/memory/m1/blame")
    assert r.status_code == 200 and r.json() == {"block_id": "b1", "fields": {"value": {"revision": 2}}}
    legacy, _ = make_client(guarded=False)
    assert legacy.get("/api/memory/m1/blame").status_code == 200


def test_route_refusals_and_no_existence_oracle():
    for params in ({}, {"from": 1}, {"from": 0, "to": 1}, {"from": 1, "to": "x"}, {"from": -1, "to": 1}, {"from": 1, "to": 1000001},
                   {"from": 1, "to": 2, "z": 1}, [("from", 1), ("from", 2), ("to", 3)]):
        client, calls = make_client()
        r = client.get("/api/memory/m1/diff", params=params)
        assert r.status_code == 422 and r.json() == {"state": "invalid_revision"} and calls == [], params
    client, calls = make_client()
    assert client.get("/api/memory/m1/blame", params={"x": 1}).status_code == 422 and calls == []
    # foreign and unknown ids are the same 404 body
    client, _ = make_client()
    a = client.get("/api/memory/gone/diff", params={"from": 1, "to": 2}); b = client.get("/api/memory/gone/blame")
    assert a.status_code == b.status_code == 404 and a.json() == b.json() == {"state": "not_found"}
    client, calls = make_client(authed=False)
    assert client.get("/api/memory/m1/blame").status_code == 403 and calls == []
    client, calls = make_client(live=False)
    r = client.get("/api/memory/m1/blame")
    assert r.status_code == 503 and r.json() == {"state": "unavailable"} and calls == []
    client, _ = make_client(provider=SimpleNamespace(_guarded_v17=False))  # un-negotiated: provider without the readers
    assert client.get("/api/memory/m1/blame").status_code == 503
    assert client.get("/api/memory/m1/diff", params={"from": 1, "to": 2}).status_code == 503


def test_zz_cleanup():
    if "w" in _CACHE:
        _CACHE.pop("w")[0].close()

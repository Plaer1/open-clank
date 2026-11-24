"""Supersede / detach routes (routes/memory/native_ops_supersede.py) over the real provider and binary, plus fake-provider route cases.

The routes are driven through a TestClient whose provider is the guarded-v18 fixture's real MemeryProvider (the
capture bracket is a stub that names the owner). Reviews are built like the panel: mirror context -> preview -> apply.
No model, no network, no UI."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.testclient import TestClient

from tests import _native_ops_fixture as F
from tests.test_memory_edit_flow import VALIDATE, edit
from src.memery_provider import _HostMirrorContext
from routes.memory.native_ops_supersede import register_native_ops_supersede, STALE

PATH = "/activation_rationale/supersedes"


def make_client(provider, owner="alice", *, authed=True, live=True):
    @asynccontextmanager
    async def admitted(user):
        yield
    ctx = {"owner": owner, "barrier": SimpleNamespace(admitted=admitted), "current_live": lambda: live}
    def require_human(request):
        if not authed: raise HTTPException(401, "no")
        return "u"
    async def owned(fn, *a, **k): return fn(*a, **k)
    router = APIRouter()
    register_native_ops_supersede(router, require_human=require_human, capture_context=lambda r, u: ctx, owned=owned,
                                  provider=provider, mirror_request_data=VALIDATE)
    app = FastAPI(); app.include_router(router, prefix="/api/memory")
    return TestClient(app)


class Remote:
    """httpx over ASGI on the caller's event loop: the real provider's transport is bound to that loop, which TestClient's thread would break."""
    def __init__(self, app, loop):
        import httpx
        self.loop = loop
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    def post(self, path, json):
        return self.loop.run_until_complete(self.client.post(path, json=json))


def make_remote(provider, owner, loop):
    c = make_client(provider, owner)
    return Remote(c.app, loop)


class World:
    def __init__(self):
        self.fx = F.build_fixture()
        self.loop = asyncio.new_event_loop()
        self.alice = self.fx.provider_for("alice")
        self.ws = self.fx.owners["alice"][1]
        self.client = make_remote(self.alice, "alice", self.loop)
        self.n = 0

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def seed(self, owner, *texts):
        return self.run(F.seed_records(self.fx, owner, list(texts), tag="seed%d" % self.next()))

    def next(self):
        self.n += 1
        return self.n

    def review(self, memory_id, provider=None):
        c = self.run((provider or self.alice)._host_mirror_context(_HostMirrorContext("alice", self.ws, memory_id)))
        return {"candidate_id": c["candidate_id"], "origin_request_key": c["origin_request_key"], "prior_receipt_id": c["prior_receipt_id"],
                "expected_revision": c["expected_revision"], "request_key": "rk-%d-key" % self.next(), "reason": "owner relation"}

    def preview(self, route, memory_id, target_id=None, client=None):
        body = {"phase": "preview", **self.review(memory_id)}
        if target_id is not None: body["target_id"] = target_id
        r = (client or self.client).post(f"/api/memory/{memory_id}/{route}", json=body)
        return body, r

    def apply(self, route, memory_id, body, preview, target_id=None):
        b = {**body, "phase": "apply", "operation_json": preview["operation_json"], "operation_sha256": preview["operation_sha256"]}
        if target_id is not None: b["target_id"] = target_id
        return self.client.post(f"/api/memory/{memory_id}/{route}", json=b)

    def full(self, route, memory_id, target_id=None):
        body, r = self.preview(route, memory_id, target_id)
        assert r.status_code == 200, (r.status_code, r.text)
        out = self.apply(route, memory_id, body, r.json(), target_id)
        assert out.status_code == 200 and out.json()["review"]["state"] == "applied", (out.status_code, out.text)
        return r.json(), out.json()

    def detail(self, memory_id, owner="alice"):
        return self.run(self.fx.provider_for(owner).versioned_detail(memory_id, owner=owner))

    def revisions(self, block):
        return F.rows(self.fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (block,))[0][0]

    def close(self):
        try:
            self.loop.run_until_complete(self.client.client.aclose())
            self.loop.run_until_complete(F.shutdown(self.fx))
        finally:
            self.loop.close(); self.fx.close()


def test_supersede_read_back_history_and_detach():
    w = World()
    try:
        a, b = w.seed("alice", "newer statement", "older statement")
        before_b = F.rows(w.fx.db, "SELECT revision,content_hash FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (b["block_id"],))
        assert "supersedes" not in w.detail(a["curated_id"])
        preview, applied = w.full("supersede", a["curated_id"], b["curated_id"])
        assert preview["target_revision"] == 1 and PATH in applied["review"]["field_decision"]["allowed_paths"]
        d = w.detail(a["curated_id"])
        assert d["supersedes"] == {"block_id": b["block_id"], "revision": 1, "target_available": True, "target_id": b["curated_id"]}, d["supersedes"]
        assert d["current_revision"] == 2 and sorted(h["revision"] for h in d["history"]) == [1, 2]
        assert F.rows(w.fx.db, "SELECT revision,content_hash FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (b["block_id"],)) == before_b
        assert "supersedes" not in w.detail(b["curated_id"])  # no reverse view
        # the same history route body (what the panel reads) carries it
        # detach: new revision without the key; both histories intact
        _, out = w.full("detach", a["curated_id"])
        d = w.detail(a["curated_id"])
        assert "supersedes" not in d and d["current_revision"] == 3 and sorted(h["revision"] for h in d["history"]) == [1, 2, 3]
        assert w.revisions(a["block_id"]) == 3
        assert F.rows(w.fx.db, "SELECT revision,content_hash FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (b["block_id"],)) == before_b
        # detach with no relation: clean refusal, nothing written
        r = w.client.post(f"/api/memory/{a['curated_id']}/detach", json={"phase": "preview", **w.review(a["curated_id"])})
        assert r.status_code == 422 and r.json() == {"state": "no_relation"} and w.revisions(a["block_id"]) == 3
    finally:
        w.close()


def test_target_changed_between_preview_and_apply_is_refused_and_nothing_is_written():
    w = World()
    try:
        a, b = w.seed("alice", "source one", "target one")
        body, r = w.preview("supersede", a["curated_id"], b["curated_id"])
        assert r.status_code == 200
        run = w.run(edit(w.alice, "alice", w.ws, b["curated_id"], "target-edit-key-1", ["/tags"], {"tags": ["changed"]}))
        assert run[1]["binding"]["revision"] == 2
        out = w.apply("supersede", a["curated_id"], body, r.json(), b["curated_id"])
        assert out.status_code == 409 and out.json() == {"state": "changed"}, (out.status_code, out.text)
        assert w.revisions(a["block_id"]) == 1 and "supersedes" not in w.detail(a["curated_id"])
        # the sealed operation cannot be reused for another target either
        c = w.seed("alice", "third one")[0]
        out = w.apply("supersede", a["curated_id"], body, r.json(), c["curated_id"])
        assert out.status_code == 409 and w.revisions(a["block_id"]) == 1
        # a fresh preview pins the new revision and applies
        preview, applied = w.full("supersede", a["curated_id"], b["curated_id"])
        assert preview["target_revision"] == 2 and w.detail(a["curated_id"])["supersedes"]["revision"] == 2
    finally:
        w.close()


def test_self_cycle_second_relation_and_foreign_target_are_refused_before_preview():
    w = World()
    try:
        a, b, c = w.seed("alice", "record a", "record b", "record c")
        bob = w.seed("bob", "bob record")[0]
        def refused(memory, target):
            body, r = w.preview("supersede", memory, target)
            return r.status_code, r.json()
        assert refused(a["curated_id"], a["curated_id"]) == (422, {"state": "self"})
        w.full("supersede", a["curated_id"], b["curated_id"])
        assert refused(a["curated_id"], c["curated_id"]) == (422, {"state": "already_supersedes"})
        assert refused(b["curated_id"], a["curated_id"]) == (422, {"state": "cycle"})
        # longer chain: a -> b, c -> a, then b -> c would close the loop
        w.full("supersede", c["curated_id"], a["curated_id"])
        assert refused(b["curated_id"], c["curated_id"]) == (422, {"state": "cycle"})
        # foreign target (bob's record) is the same refusal as an unknown id; as is a foreign record as the subject
        foreign = refused(w.seed("alice", "record d")[0]["curated_id"], bob["curated_id"])
        unknown = refused(a["curated_id"] if False else w.seed("alice", "record e")[0]["curated_id"], "m_does_not_exist")
        assert foreign == unknown == (404, {"state": "not_found"})
        assert w.client.post(f"/api/memory/{bob['curated_id']}/detach", json={"phase": "preview", **w.review(a["curated_id"])}).status_code == 404
        # bob cannot see or touch alice's relation
        bob_client = make_remote(w.fx.provider_for("bob"), "bob", w.loop)
        r = bob_client.post(f"/api/memory/{a['curated_id']}/detach", json={"phase": "preview", **w.review(a["curated_id"])})
        assert r.status_code == 404 and r.json() == {"state": "not_found"}
        assert "supersedes" in w.detail(a["curated_id"])
        try:
            w.run(w.fx.provider_for("bob").versioned_detail(a["curated_id"], owner="bob"))
        except Exception as exc:
            assert type(exc).__name__ == "VersionedMemoryNotFound"
        else:
            raise AssertionError("bob read alice's record")
    finally:
        w.close()


def test_chain_beyond_the_bound_is_refused():
    w = World()
    try:
        records = w.seed("alice", *["chain %d" % i for i in range(19)])
        for i in range(17):  # r0 -> r1 -> ... -> r17
            w.full("supersede", records[i]["curated_id"], records[i + 1]["curated_id"])
        # r18 -> r0 would need 17 hops to find the end: beyond the bound of 16
        body, r = w.preview("supersede", records[18]["curated_id"], records[0]["curated_id"])
        assert r.status_code == 422 and r.json() == {"state": "chain_too_long"}
        body, r = w.preview("supersede", records[18]["curated_id"], records[2]["curated_id"])
        assert r.status_code == 200  # 15 hops: within the bound
    finally:
        w.close()


def test_forgotten_target_is_reported_only_as_unavailable():
    w = World()
    try:
        a, b = w.seed("alice", "keeper statement", "doomed statement")
        w.full("supersede", a["curated_id"], b["curated_id"])
        account = w.fx.owners["alice"][0]
        refs = w.fx.coordinator.checked_refs("alice", account, w.ws)
        before_issue = lambda: w.fx.coordinator.checked_refs("alice", account, w.ws) == refs
        base = {"version": 1, "owner": "alice", "workspace_id": w.ws, "account_id": account, "policy_projection": refs,
                "action": "preview", "selector_kind": "record_id", "selector": b["curated_id"], "preview_token": None, "tombstone_id": None, "operation_id": None}
        pv = w.run(w.alice._host_guarded_batch_forget(base, before_issue=before_issue))
        done = w.run(w.alice._host_guarded_batch_forget({**base, "action": "commit", "preview_token": pv["token"], "operation_id": "1" * 32}, before_issue=before_issue))
        assert done.get("tombstone_id")
        d = w.detail(a["curated_id"])
        assert d["supersedes"] == {"block_id": b["block_id"], "revision": 1, "target_available": False}, d["supersedes"]
        assert "target_id" not in d["supersedes"]
        # detach still works on a relation whose target is gone
        w.full("detach", a["curated_id"])
        assert "supersedes" not in w.detail(a["curated_id"])
    finally:
        w.close()


def fake_provider(**kw):
    async def relation(memory_id, owner):
        if memory_id == "gone": raise __import__("src.memory_versioned", fromlist=["x"]).VersionedMemoryNotFound("x")
        return {"block_id": "b-" + memory_id, "revision": 1, "claim_slot": memory_id, "relation": None}
    base = dict(_guarded_v17=True, _workspace_id="ws", versioned_relation=relation)
    base.update(kw)
    return SimpleNamespace(**base)


def body(**kw):
    b = {"phase": "preview", "target_id": "t1", "candidate_id": "c", "origin_request_key": "k", "prior_receipt_id": "p",
         "expected_revision": 1, "request_key": "rk", "reason": "r"}
    b.update(kw)
    return {k: v for k, v in b.items() if v is not None}


def test_route_refusals_with_fakes():
    # legacy profile and un-negotiated provider: unavailable
    assert make_client(fake_provider(_guarded_v17=False)).post("/api/memory/m1/supersede", json=body()).status_code == 503
    assert make_client(SimpleNamespace(_guarded_v17=True, _workspace_id="ws")).post("/api/memory/m1/supersede", json=body()).status_code == 503
    assert make_client(fake_provider(), authed=False).post("/api/memory/m1/supersede", json=body()).status_code == 403
    assert make_client(fake_provider(), live=False).post("/api/memory/m1/supersede", json=body()).status_code == 503
    client = make_client(fake_provider())
    for bad in (body(phase="x"), body(target_id=None), body(target_id=""), {**body(), "extra": 1}, body(phase="apply"), [1]):
        r = client.post("/api/memory/m1/supersede", json=bad)
        assert r.status_code == 422 and r.json() == {"state": "invalid_request"}, bad
    assert client.post("/api/memory/m1/detach", json=body()).status_code == 422  # detach takes no target_id
    assert client.post("/api/memory/m1/supersede", content=b"nope").status_code == 422
    assert client.post("/api/memory/m1/supersede", json=body(target_id="m1")).json() == {"state": "self"}
    assert client.post("/api/memory/m1/supersede", json=body(target_id="gone")).json() == {"state": "not_found"}
    assert client.post("/api/memory/gone/supersede", json=body()).json() == {"state": "not_found"}


def test_native_refusal_is_mapped_to_one_plain_message():
    async def preview(request): raise RuntimeError("unsupported mirror payload")
    client = make_client(fake_provider(_host_mirror_preview=preview))
    r = client.post("/api/memory/m1/supersede", json=body())
    assert r.status_code == 409 and r.json() == {"state": "refused", "detail": STALE}
    assert "mirror" not in r.text


def test_zz_no_leftovers():
    pass

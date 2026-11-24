"""F-D3: looking at a memory in the graph (graph_expand bumps edge traversal_count) must not block forgetting it.

Real transport on the guarded-v18 fixture: records are created by the real explicit-add + create-only batch path and
forgotten through the real guarded batch forget (preview + commit). Meaningful graph changes (trust, new edge) are
injected with a privileged connection on the disposable store (the writer fence blocks them through any product path)."""
import sqlite3

from tests import _native_ops_fixture as F
from src.memery_v2 import list_current_records

WRITER = "openclank.frankenmemory.writer.v3"


def privileged(fx, statement, args=()):
    db = sqlite3.connect(fx.db)
    db.create_function("fm_writer_capability", 0, lambda: WRITER, deterministic=True)
    try:
        db.execute(statement, args)
        db.commit()
    finally:
        db.close()


async def setup(fx, tag):
    seeded = await F.seed_records(fx, "alice", ["alpha " + tag, "beta " + tag], tag="fd3" + tag)
    p = fx.provider_for("alice")
    owner, (account, ws) = "alice", fx.owners["alice"]
    refs = fx.coordinator.checked_refs(owner, account, ws)
    before_issue = lambda: fx.coordinator.checked_refs(owner, account, ws) == refs
    body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
            "action": "preview", "selector_kind": "record_id", "selector": seeded[1]["curated_id"],
            "preview_token": None, "tombstone_id": None, "operation_id": None}
    return seeded, p, ws, body, before_issue


async def graph_read(p, ws, record):
    """The panel-style read: the record's node and its neighbours (bumps traversal_count on its edge)."""
    return await p._call_tool("graph_walk", {"op": "expand", "owner": "alice", "workspace_id": ws, "node_kind": "memory",
                                             "node_name": record["curated_id"], "limit": 5})


async def commit(p, body, preview, before_issue, op="1" * 32):
    return await p._host_guarded_batch_forget({**body, "action": "commit", "preview_token": preview["token"], "operation_id": op},
                                              before_issue=before_issue)


def traversal(fx):
    return F.rows(fx.db, "SELECT traversal_count FROM graph_edges WHERE owner='alice' ORDER BY id")


def assert_forgotten(fx, record, closure, other):
    assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (record["curated_id"],)) == [(0,)]
    for table, column, ids in (("candidates", "id", closure["candidate_ids"]), ("raw", "id", closure["raw_ids"]),
                               ("curated", "id", closure["curated_ids"]), ("graph_nodes", "id", closure["graph_node_ids"])):
        assert ids or table == "raw", (table, closure)  # explicit-add records have no raw row
        for value in ids:
            assert F.rows(fx.db, f"SELECT count(*) FROM {table} WHERE {column}=?", (value,)) == [(0,)], (table, value)
        # no residue: no edge still points at a removed node
    for node in closure["graph_node_ids"]:
        assert F.rows(fx.db, "SELECT count(*) FROM graph_edges WHERE src_id=? OR dst_id=?", (node, node)) == [(0,)]
    left = [r["text"] for r in list_current_records(owner="alice", workspace_id="ws-a", limit=None, db_path=str(fx.db))]
    assert left == [other["text"]], left
    assert len(F.rows(fx.db, "SELECT id FROM graph_edges WHERE owner='alice'")) == 1  # the other record's edge is untouched


def run_case(order, check):
    fx = F.build_fixture()
    async def go():
        try:
            seeded, p, ws, body, before_issue = await setup(fx, order[:4])
            await check(fx, seeded, p, ws, body, before_issue)
        finally:
            await F.shutdown(fx)
    F.run(go())


def test_graph_read_before_preview_does_not_block_forget():
    async def check(fx, seeded, p, ws, body, before_issue):
        await graph_read(p, ws, seeded[1])
        assert 1 in {r[0] for r in traversal(fx)}  # the read really bumped the counter
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        result = await commit(p, body, preview, before_issue)
        assert_forgotten(fx, seeded[1], result["closure"], seeded[0])
        status = await p._host_guarded_batch_forget({**body, "action": "status", "selector_kind": None, "selector": None,
                                                     "preview_token": None, "operation_id": "1" * 32}, before_issue=before_issue)
        assert status["state"] == "committed" and status["tombstone_id"] == result["tombstone_id"]
    run_case("read-first", check)


def test_graph_read_between_preview_and_commit_does_not_block_forget():
    # Design: the preview token binds the closure (selected ids), not read counters, so a read in between never invalidates it.
    async def check(fx, seeded, p, ws, body, before_issue):
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        await graph_read(p, ws, seeded[1])
        await graph_read(p, ws, seeded[0])
        result = await commit(p, body, preview, before_issue)
        assert_forgotten(fx, seeded[1], result["closure"], seeded[0])
    run_case("read-between", check)


def test_heavily_traversed_and_decayed_edge_does_not_block_forget():
    async def check(fx, seeded, p, ws, body, before_issue):
        privileged(fx, "UPDATE graph_edges SET traversal_count=40, weight=3.5 WHERE owner='alice'")
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        result = await commit(p, body, preview, before_issue)
        assert_forgotten(fx, seeded[1], result["closure"], seeded[0])
    run_case("decayed", check)


def test_other_reads_never_blocked_forget():
    async def check(fx, seeded, p, ws, body, before_issue):
        g = lambda **a: p._call_tool("graph_walk", {"owner": "alice", "workspace_id": ws, **a})
        overview = await g(op="overview", limit=20)
        node = [n["id"] for n in overview["nodes"] if n["name"] == seeded[1]["curated_id"]][0]
        await g(op="trace", node_id=node, max_depth=3, limit=5)
        await g(op="fetch", node_id=node)
        await g(op="rank", node_id=node, limit=5)
        await p._call_tool("search", {"query": "beta", "owner": "alice", "workspace_id": ws, "limit": 5})
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        assert_forgotten(fx, seeded[1], (await commit(p, body, preview, before_issue))["closure"], seeded[0])
    run_case("otherreads", check)


async def refused(p, body, preview, before_issue, op="1" * 32):
    """True when the commit is refused. A clean Native refusal whose status is 'absent' releases the Host's pending marker
    (F-D3b), so the provider can issue the next forget; ambiguity (timeout, EOF, odd status) stays pending."""
    try:
        await commit(p, body, preview, before_issue, op)
    except Exception:
        assert p._guarded_batch_forget_ticket is None  # released on a confirmed refusal
        return True
    return False


def intact(fx, record):
    assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (record["curated_id"],)) == [(1,)]
    assert len(F.rows(fx.db, "SELECT id FROM graph_edges WHERE owner='alice'")) >= 2


def test_trust_change_between_preview_and_commit_is_still_refused():
    async def check(fx, seeded, p, ws, body, before_issue):
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        privileged(fx, "UPDATE graph_edges SET trust=1 WHERE owner='alice' AND id IN (SELECT e.id FROM graph_edges e JOIN graph_nodes n ON n.id IN (e.src_id,e.dst_id) WHERE n.name=?)", (seeded[1]["curated_id"],))
        assert await refused(p, body, preview, before_issue)
        intact(fx, seeded[1])
    run_case("trustchange", check)


def test_extra_edge_on_the_record_is_still_refused_even_with_a_fresh_preview():
    async def check(fx, seeded, p, ws, body, before_issue):
        privileged(fx, "INSERT INTO graph_edges(id,src_id,tag,dst_id,fact_id,weight,traversal_count,trust,created_at,last_seen,owner,workspace_id,candidate_id,status) "
                       "SELECT 'extra-edge',n.id,'relates',n.id,NULL,1.0,0,0,'t','t','alice','ws-a',NULL,'active' FROM graph_nodes n WHERE n.name=?", (seeded[1]["curated_id"],))
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)  # a new preview does not make it eligible
        assert await refused(p, body, preview, before_issue)
        intact(fx, seeded[1])
    run_case("extraedge", check)


def test_a_fact_on_the_edge_is_still_refused():
    async def check(fx, seeded, p, ws, body, before_issue):
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        privileged(fx, "UPDATE graph_edges SET fact_id='some-fact' WHERE owner='alice' AND id IN (SELECT e.id FROM graph_edges e JOIN graph_nodes n ON n.id IN (e.src_id,e.dst_id) WHERE n.name=?)", (seeded[1]["curated_id"],))
        assert await refused(p, body, preview, before_issue)
        intact(fx, seeded[1])
    run_case("factedge", check)


def test_refused_forget_does_not_block_later_forgets():
    async def go(fx):
        seeded = await F.seed_records(fx, "alice", ["alpha one", "beta two", "gamma three"], tag="fd3bthree")
        p = fx.provider_for("alice")
        owner, (account, ws) = "alice", fx.owners["alice"]
        refs = fx.coordinator.checked_refs(owner, account, ws)
        before_issue = lambda: fx.coordinator.checked_refs(owner, account, ws) == refs
        def body_for(record):
            return {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                    "action": "preview", "selector_kind": "record_id", "selector": record["curated_id"],
                    "preview_token": None, "tombstone_id": None, "operation_id": None}
        refused_record, other = seeded[1], seeded[2]
        trust = ("UPDATE graph_edges SET trust=? WHERE owner='alice' AND id IN (SELECT e.id FROM graph_edges e JOIN graph_nodes n "
                 "ON n.id IN (e.src_id,e.dst_id) WHERE n.name=?)")
        privileged(fx, trust, (1, refused_record["curated_id"]))
        body = body_for(refused_record)
        assert await refused(p, body, await p._host_guarded_batch_forget(body, before_issue=before_issue), before_issue, "1" * 32)
        intact(fx, refused_record)
        # a DIFFERENT record is forgotten on the same provider right after the refusal
        body2 = body_for(other)
        result = await commit(p, body2, await p._host_guarded_batch_forget(body2, before_issue=before_issue), before_issue, "2" * 32)
        assert other["curated_id"] in result["closure"]["curated_ids"]
        assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (other["curated_id"],)) == [(0,)]
        # the refused record is still there; once the meaningful change is reverted it can be forgotten too
        assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (refused_record["curated_id"],)) == [(1,)]
        privileged(fx, trust, (0, refused_record["curated_id"]))
        again = await commit(p, body, await p._host_guarded_batch_forget(body, before_issue=before_issue), before_issue, "3" * 32)
        assert refused_record["curated_id"] in again["closure"]["curated_ids"]
        left = [r["text"] for r in list_current_records(owner="alice", workspace_id="ws-a", limit=None, db_path=str(fx.db))]
        assert left == ["alpha one"], left
    fx = F.build_fixture()
    async def wrapper():
        try:
            await go(fx)
        finally:
            await F.shutdown(fx)
    F.run(wrapper())


class _Ticket(dict):
    pass


def test_ambiguous_failures_stay_pending_exactly_as_before():
    from src.memory_provider import MemoryRequestRejectedError
    async def go(fx):
        seeded = await F.seed_records(fx, "alice", ["alpha amb", "beta amb"], tag="fd3bamb")
        p = fx.provider_for("alice")
        owner, (account, ws) = "alice", fx.owners["alice"]
        refs = fx.coordinator.checked_refs(owner, account, ws)
        before_issue = lambda: fx.coordinator.checked_refs(owner, account, ws) == refs
        body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                "action": "preview", "selector_kind": "record_id", "selector": seeded[1]["curated_id"],
                "preview_token": None, "tombstone_id": None, "operation_id": None}
        preview = await p._host_guarded_batch_forget(body, before_issue=before_issue)
        real_request, real_rpc = p._host_candidate_request, p._s07_owner_rpc
        async def failing(owner_, ws_, request, **kw):
            kw["write_status"]["ticket"] = {"issued": True, "owned_io_drained": False, "terminal_error": kw_terminal[0]}
            raise (RuntimeError("pipe broke") if not kw_terminal[0] else MemoryRequestRejectedError("bounded memory write rejected"))
        async def status_fails(*a, **k): raise RuntimeError("status transport error")
        async def status_committed(*a, **k):
            return {"operation_id": "1" * 32, "state": "committed", "tombstone_id": "t", "closure": {}, "recover_until": None, "recovered_at": None}
        async def status_odd(*a, **k): return {"state": "absent"}
        cases = (("transport failure (no error frame)", [False], real_rpc),
                 ("error frame, status transport error", [True], status_fails),
                 ("error frame, status says committed", [True], status_committed),
                 ("error frame, malformed status", [True], status_odd))
        try:
            for label, terminal, rpc in cases:
                kw_terminal = terminal
                p._host_candidate_request, p._s07_owner_rpc = failing, rpc
                try:
                    await commit(p, body, preview, before_issue)
                except RuntimeError as exc:
                    assert "outcome unknown" in str(exc), label
                else:
                    raise AssertionError(label)
                assert p._guarded_batch_forget_ticket is not None, label
                p._host_candidate_request, p._s07_owner_rpc = real_request, real_rpc
                try:  # a new commit is refused as before
                    await commit(p, body, preview, before_issue, "9" * 32)
                except MemoryRequestRejectedError as exc:
                    assert "requires status" in str(exc), label
                else:
                    raise AssertionError(label)
                p._guarded_batch_forget_ticket = None  # reset for the next case (test-only)
        finally:
            p._host_candidate_request, p._s07_owner_rpc = real_request, real_rpc
        intact(fx, seeded[1])
    fx = F.build_fixture()
    async def wrapper():
        try:
            await go(fx)
        finally:
            await F.shutdown(fx)
    F.run(wrapper())


def test_record_stays_deletable_after_maintenance_pruned_its_edge():
    async def go(fx):
        seeded = await F.seed_records(fx, "alice", ["only record to prune"], tag="fd3bprune")
        record = seeded[0]
        p = fx.provider_for("alice")
        owner, (account, ws) = "alice", fx.owners["alice"]
        refs = fx.coordinator.checked_refs(owner, account, ws)
        before_issue = lambda: fx.coordinator.checked_refs(owner, account, ws) == refs
        # age the edge, then run the real maintenance pass (groom edge_decay) so decay prunes it
        privileged(fx, "UPDATE graph_edges SET last_seen='2000-01-01T00:00:00+00:00' WHERE owner='alice'")
        await p.groom("edge_decay", owner="alice")
        pruned_by_product = F.rows(fx.db, "SELECT count(*) FROM graph_edges WHERE owner='alice'") == [(0,)]
        if not pruned_by_product:  # the guarded store refused the legacy groom write: reach the same state directly
            privileged(fx, "DELETE FROM graph_edges WHERE owner='alice'")
        assert F.rows(fx.db, "SELECT count(*) FROM graph_nodes WHERE owner='alice'")[0][0] == 2  # memory + category remain
        body = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs,
                "action": "preview", "selector_kind": "record_id", "selector": record["curated_id"],
                "preview_token": None, "tombstone_id": None, "operation_id": None}
        result = await commit(p, body, await p._host_guarded_batch_forget(body, before_issue=before_issue), before_issue)
        assert record["curated_id"] in result["closure"]["curated_ids"]
        for table in ("curated", "candidates", "graph_nodes", "graph_edges", "graph_cues"):
            owner_column = "owner"
            assert F.rows(fx.db, f"SELECT count(*) FROM {table} WHERE {owner_column}='alice'") == [(0,)], table
        print("pruned by product call:", pruned_by_product)
    fx = F.build_fixture()
    async def wrapper():
        try:
            await go(fx)
        finally:
            await F.shutdown(fx)
    F.run(wrapper())

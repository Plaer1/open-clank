"""F-D5: the panel's reviewed edit of a memory created through explicit add or the create-only batch, over the real transport.

The route's own request validator (`_mirror_request_data`, extracted by AST from routes/memory/memory_routes.py) feeds
`_host_mirror_context` -> `_host_mirror_preview` -> `_host_mirror_apply`, exactly as `POST /{id}/mirror-preview` and
`PUT /{id}` drive them. Before the fix Native refused every such edit with `unknown field request_sha256` because the
origin receipt of an explicit-add record is the account envelope, not the older bare candidate receipt."""
import ast
import json
import re
from pathlib import Path

from fastapi import HTTPException

from tests import _native_ops_fixture as F
from src.memery_provider import _HostMirrorApply, _HostMirrorContext, _HostMirrorPreview
from src.memery_v2 import count_current_records, list_current_records, search_current_records
from src.memory_provider import MemoryRequestRejectedError

REPO = Path(__file__).resolve().parent.parent


def route_validator():
    tree = ast.parse((REPO / "routes/memory/memory_routes.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_mirror_request_data":
            node.decorator_list, node.returns = [], None
            for arg in node.args.args + node.args.kwonlyargs:
                arg.annotation = None
            ns = {"HTTPException": HTTPException, "re": re}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), "memory_routes.py", "exec"), ns)
            return ns["_mirror_request_data"]
    raise AssertionError("route validator not found")


VALIDATE = route_validator()


async def edit(p, owner, ws, memory_id, key, paths, payload, reason="panel edit"):
    """Context -> validated preview request -> preview -> validated apply request -> apply (what the two routes do)."""
    c = await p._host_mirror_context(_HostMirrorContext(owner, ws, memory_id))
    body = VALIDATE({"candidate_id": c["candidate_id"], "origin_request_key": c["origin_request_key"], "prior_receipt_id": c["prior_receipt_id"],
                     "expected_revision": c["expected_revision"], "request_key": key, "reason": reason, "selected_paths": paths, "payload": payload}, apply=False)
    preview = await p._host_mirror_preview(_HostMirrorPreview(owner, ws, memory_id, body["candidate_id"], body["origin_request_key"],
        body["prior_receipt_id"], body["expected_revision"], body["request_key"], body["reason"], tuple(body["selected_paths"]), body["payload"]))
    abody = VALIDATE({**{k: body[k] for k in ("candidate_id", "origin_request_key", "prior_receipt_id", "expected_revision", "request_key", "reason", "selected_paths")},
                      "operation_json": preview["operation_json"], "operation_sha256": preview["operation_sha256"]}, apply=True)
    return preview, await p._host_mirror_apply(_HostMirrorApply(owner, ws, memory_id, abody["candidate_id"], abody["origin_request_key"],
        abody["prior_receipt_id"], abody["expected_revision"], abody["request_key"], abody["reason"], tuple(abody["selected_paths"]),
        abody["operation_json"], abody["operation_sha256"]))


async def add_directly(fx, owner, text, key):
    """The single explicit-add path in manual mode (publishes without the batch). Returns the curated id."""
    account, ws = fx.owners[owner]
    p, coordinator = fx.provider_for(owner), fx.coordinator
    await p.initialize()
    assert await p._host_negotiate_explicit_add(owner=owner, workspace_id=ws) is True
    refs = coordinator.checked_refs(owner, account, ws)
    result = await p._host_explicit_add({"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "request_key": key,
        "policy_projection": refs, "content": text, "category": "fact", "source": "user", "question_context": None, "mode": "manual"},
        before_issue=lambda: coordinator.checked_refs(owner, account, ws) == refs)
    assert result["state"] == "applied", result
    rows = [r for r in list_current_records(owner=owner, workspace_id=ws, limit=None, db_path=str(fx.db)) if r["text"] == text]
    assert len(rows) == 1
    return rows[0]["id"]


def visible(fx, owner="alice", ws="ws-a"):
    """What the panel list shows: the product reader (`require_legacy_presence=True`, src/memery_provider.py:2322,2448): a v2 row is
    current only while its source event still has a live curated record (src/memery_v2.py:2577-2582)."""
    return list_current_records(owner=owner, workspace_id=ws, limit=None, require_legacy_presence=True, db_path=str(fx.db))


def raw_visible(fx, owner="alice", ws="ws-a"):
    """Canonical rows only, no legacy lineage filter: for tests that deliberately count canonical residue."""
    return list_current_records(owner=owner, workspace_id=ws, limit=None, db_path=str(fx.db))


def revision_count(fx, owner, memory_id):
    return F.rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions r JOIN fm_v2_knowledge_blocks b ON b.owner_id=r.owner_id AND b.block_id=r.block_id "
                         "WHERE r.owner_id=? AND b.claim_slot=?", (owner, memory_id))[0][0]


def run_case(body):
    fx = F.build_fixture()
    async def go():
        try:
            await body(fx)
        finally:
            await F.shutdown(fx)
    F.run(go())


async def check_edit_cycle(fx, memory_id, label):
    p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
    before = [r["text"] for r in visible(fx)]
    assert revision_count(fx, "alice", memory_id) == 1
    # first edit: /tags (origin = the explicit-add receipt)
    preview, applied = await edit(p, "alice", ws, memory_id, label + "-tags-0001", ["/tags"], {"tags": ["alpha", "beta"]})
    assert applied["state"] == "applied" and applied["binding"]["revision"] == 2
    row = [r for r in visible(fx) if r["id"] == memory_id][0]
    assert row["tags"] == ["alpha", "beta"] and row["text"] in before
    assert revision_count(fx, "alice", memory_id) == 2
    # second edit: /value/value (origin chain through the first update receipt)
    new_text = "edited " + label
    _, applied = await edit(p, "alice", ws, memory_id, label + "-text-0002", ["/value/value"], {"value": {"type": "string", "value": new_text}})
    assert applied["binding"]["revision"] == 3
    rows = [r for r in visible(fx) if r["id"] == memory_id]
    assert len(rows) == 1 and rows[0]["text"] == new_text and rows[0]["tags"] == ["alpha", "beta"]
    assert revision_count(fx, "alice", memory_id) == 3
    # one record still: list, count and search agree
    assert len(visible(fx)) == len(before) and count_current_records(owner="alice", workspace_id="ws-a", db_path=str(fx.db)) == len(before)
    hits = search_current_records(new_text, owner="alice", workspace_id="ws-a", db_path=str(fx.db))
    assert [h["id"] for h in hits][:1] == [memory_id]


def test_batch_created_record_can_be_edited_twice_and_stays_one_record():
    async def body(fx):
        seeded = await F.seed_records(fx, "alice", ["batch created memory"], tag="d5batch")
        await check_edit_cycle(fx, seeded[0]["curated_id"], "batch")
    run_case(body)


def test_directly_added_record_can_be_edited_twice_and_stays_one_record():
    async def body(fx):
        memory_id = await add_directly(fx, "alice", "directly added memory", "d5-direct-add-key-0001")
        await check_edit_cycle(fx, memory_id, "direct")
    run_case(body)


def test_another_owner_cannot_preview_or_apply_and_gets_the_unknown_id_refusal():
    async def body(fx):
        seeded = await F.seed_records(fx, "alice", ["alice private memory"], tag="d5own")
        await F.seed_records(fx, "bob", ["bob memory"], tag="d5bob")
        alice_id = seeded[0]["curated_id"]
        a_ws, b_ws = fx.owners["alice"][1], fx.owners["bob"][1]
        pa, pb = fx.provider_for("alice"), fx.provider_for("bob")
        c = await pa._host_mirror_context(_HostMirrorContext("alice", a_ws, alice_id))
        async def attempt(provider, owner, ws, memory_id):
            try:
                await provider._host_mirror_preview(_HostMirrorPreview(owner, ws, memory_id, c["candidate_id"], c["origin_request_key"],
                    c["prior_receipt_id"], c["expected_revision"], "intruder-key-0001", "reason", ("/tags",), {"tags": ["x"]}))
            except Exception as exc:
                return type(exc).__name__
            return "accepted"
        foreign = await attempt(pb, "bob", b_ws, alice_id)
        unknown = await attempt(pb, "bob", b_ws, "m_does_not_exist")
        assert foreign == unknown != "accepted", (foreign, unknown)
        try:
            await pb._host_mirror_context(_HostMirrorContext("bob", b_ws, alice_id))
        except Exception as exc:
            foreign_ctx = type(exc).__name__
        else:
            raise AssertionError("foreign context accepted")
        try:
            await pb._host_mirror_context(_HostMirrorContext("bob", b_ws, "m_does_not_exist"))
        except Exception as exc:
            assert type(exc).__name__ == foreign_ctx
        assert [r["text"] for r in visible(fx)] == ["alice private memory"]  # alice's record untouched
    run_case(body)


WRITER = "openclank.frankenmemory.writer.v3"


def privileged(fx, statement, args=()):
    import sqlite3
    db = sqlite3.connect(fx.db)
    db.create_function("fm_writer_capability", 0, lambda: WRITER, deterministic=True)
    try:
        db.execute(statement, args)
        db.commit()
    finally:
        db.close()


def forget_request(fx, record, action, *, token=None, operation=None):
    account, ws = fx.owners["alice"]
    refs = fx.coordinator.checked_refs("alice", account, ws)
    return {"version": 1, "owner": "alice", "workspace_id": ws, "account_id": account, "policy_projection": refs, "action": action,
            "selector_kind": "record_id" if action in ("preview", "commit") else None, "selector": record["curated_id"] if action in ("preview", "commit") else None,
            "preview_token": token, "tombstone_id": None, "operation_id": operation}


async def forget_preview(fx, record):
    account, ws = fx.owners["alice"]
    p = fx.provider_for("alice")
    return await p._host_guarded_batch_forget(forget_request(fx, record, "preview"), before_issue=lambda: True)


async def forget_commit(fx, record, preview, operation):
    p = fx.provider_for("alice")
    return await p._host_guarded_batch_forget(forget_request(fx, record, "commit", token=preview["token"], operation=operation), before_issue=lambda: True)


def residue(fx, record):
    """Row counts that must all be zero once the record is forgotten (table by table)."""
    block, curated, candidate = record["block_id"], record["curated_id"], record["candidate_id"]
    q = lambda sql, *args: F.rows(fx.db, sql, args)[0][0]
    out = {
        "blocks": q("SELECT count(*) FROM fm_v2_knowledge_blocks WHERE block_id=?", block),
        "revisions": q("SELECT count(*) FROM fm_v2_knowledge_revisions WHERE block_id=?", block),
        "revision_evidence": q("SELECT count(*) FROM fm_v2_revision_evidence WHERE block_id=?", block),
        "field_authority": q("SELECT count(*) FROM fm_v2_revision_field_authority WHERE block_id=?", block),
        "revision_metadata": q("SELECT count(*) FROM fm_v2_revision_metadata WHERE block_id=?", block),
        "outbox_mentions": q("SELECT count(*) FROM fm_v2_outbox WHERE payload_json LIKE ? OR payload_json LIKE ?", "%" + block + "%", "%" + curated + "%"),
        "conflicts": q("SELECT count(*) FROM fm_v2_conflicts WHERE block_id=?", block),
        "evidence": q("SELECT count(*) FROM fm_v2_evidence WHERE evidence_id LIKE ?", "evidence_" + curated + "_%"),
        "sources": q("SELECT count(*) FROM fm_v2_sources WHERE source_uri=?", "memory://" + curated),
        "candidates": q("SELECT count(*) FROM candidates WHERE id=?", candidate),
        "v2_candidates": q("SELECT count(*) FROM fm_v2_candidates WHERE candidate_id=?", candidate),
        "candidate_revisions": q("SELECT count(*) FROM fm_v2_candidate_revisions WHERE candidate_id=?", candidate),
        "curated": q("SELECT count(*) FROM curated WHERE id=?", curated),
        "graph_nodes": q("SELECT count(*) FROM graph_nodes WHERE name=? OR ref_id=?", curated, curated),
        "graph_cues": q("SELECT count(*) FROM graph_cues WHERE node_id IN (SELECT id FROM graph_nodes WHERE name=?)", curated),
    }
    return out


def change_sets(fx, kinds=("panel edit", "reviewed mirror update")):
    return F.rows(fx.db, "SELECT count(*) FROM fm_v2_change_sets WHERE reason IN (%s)" % ",".join("?" * len(kinds)), kinds)[0][0]


async def edit_history(p, fx, record):
    """Four edits across the paths: /tags, /value/value, supersedes set (target: sibling), supersedes detach."""
    ws = fx.owners["alice"][1]
    mid = record["curated_id"]
    await edit(p, "alice", ws, mid, "hist-tags-0001", ["/tags"], {"tags": ["one", "two"]})
    await edit(p, "alice", ws, mid, "hist-text-0002", ["/value/value"], {"value": {"type": "string", "value": "rewritten with other words"}})
    return mid


# BQ-6 (reverted 11-12): the F-D6 "erase after edit" no longer exists in Native. Plan (guarded-forget.md:5): "Unsupported shared/unknown
# dependencies refuse safely and are disclosed": an EDITED batch member is refused and nothing is deleted; the sibling stays editable.
def test_an_edited_batch_member_is_refused_by_the_guarded_forget_and_the_sibling_survives():
    async def body(fx):
        seeded = await F.seed_records(fx, "alice", ["sibling stays", "edited then refused"], tag="d6hist")
        sibling, edited = seeded
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        mid = await edit_history(p, fx, edited)
        before, sibling_before = residue(fx, edited), residue(fx, sibling)
        try:
            preview = await forget_preview(fx, edited)
            await forget_commit(fx, edited, preview, "1" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("an edited batch member was erased")
        assert residue(fx, edited) == before and residue(fx, sibling) == sibling_before
        assert sorted(r["text"] for r in visible(fx)) == ["rewritten with other words", "sibling stays"]
        _, applied = await edit(p, "alice", ws, sibling["curated_id"], "after-refusal-0006", ["/tags"], {"tags": ["still works"]})
        assert applied["state"] == "applied"
    run_case(body)


def test_an_edit_between_preview_and_commit_makes_the_commit_refuse_as_stale():
    # BQ-6: the first half of this test (preview of an already edited record, then a fresh preview that commits) depended on F-D6
    # erase-after-edit and is removed; an edited batch member now refuses at preview (see the test above). What remains: a record
    # with NO edit at preview time whose first edit lands before commit is refused as stale and stays.
    async def body(fx):
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        seeded = await F.seed_records(fx, "alice", ["first edit lands late"], tag="d6late")
        late = seeded[0]
        preview = await forget_preview(fx, late)
        await edit(p, "alice", ws, late["curated_id"], "late-edit-0003", ["/tags"], {"tags": ["x"]})
        try:
            await forget_commit(fx, late, preview, "3" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("commit accepted a preview taken before the first edit")
        assert residue(fx, late)["curated"] == 1
    run_case(body)


def test_an_uncertain_chain_is_refused_and_nothing_is_deleted():
    async def body(fx):
        seeded = await F.seed_records(fx, "alice", ["gap probe one", "unknown receipt probe"], tag="d6gap")
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        gap, unknown = seeded
        for record, key in ((gap, "gap"), (unknown, "unk")):
            await edit(p, "alice", ws, record["curated_id"], key + "-tags-0001", ["/tags"], {"tags": ["a"]})
            await edit(p, "alice", ws, record["curated_id"], key + "-text-0002", ["/value/value"], {"value": {"type": "string", "value": key + " rewritten words"}})
        # (a) a gap: the outbox event of one update is missing
        privileged(fx, "DELETE FROM fm_v2_outbox WHERE kind='mirror_update' AND json_extract(payload_json,'$.block_id')=? AND source_revision=2", (gap["block_id"],))
        # (b) an update receipt of a family the plan does not know
        import hashlib, json as _json
        (cid, raw) = F.rows(fx.db, "SELECT c.change_set_id, c.operation_metadata_json FROM fm_v2_change_sets c JOIN fm_v2_outbox o ON o.change_set_id=c.change_set_id AND o.owner_id=c.owner_id "
                                   "WHERE o.kind='mirror_update' AND json_extract(o.payload_json,'$.block_id')=? AND o.source_revision=3", (unknown["block_id"],))[0]
        changed = raw.replace("s03.mirror-update.v1", "s99.unknown-update.v1", 1)
        privileged(fx, "UPDATE fm_v2_change_sets SET operation_metadata_json=?, content_hash=? WHERE change_set_id=?", (changed, hashlib.sha256(changed.encode()).hexdigest(), cid))
        for n, record in enumerate((gap, unknown)):
            before = residue(fx, record)
            try:  # BQ-6: an edited batch member is refused already at preview; a refusal anywhere before effects is what is asserted
                preview = await forget_preview(fx, record)
                await forget_commit(fx, record, preview, str(n + 1) * 32)
            except Exception:
                pass
            else:
                raise AssertionError("an uncertain chain was deleted")
            assert residue(fx, record) == before
    run_case(body)


# BQ-6: the F-D6 bound tests (59 edits forgettable / 60 refused with `forget_history_too_large`) are removed: Native no longer has that
# reason or erase-after-edit.

# ---- F-D7: forgetting leaves nothing of the record readable through any graph read -----------------------------------

async def graph_sweep(p, owner, ws, seen=()):
    """Every graph read the panel and the tools have, plus an expand/fetch of each node id ever seen.
    Returns ((node_total, edge_total), the whole answer as text, the node ids present now)."""
    out, ids = [], set()
    async def call(coro):
        try:
            out.append(await coro)
        except Exception as exc:  # an unknown id may be refused: it must not echo text either
            out.append(type(exc).__name__)
        return out[-1]
    ov = await p.graph("overview", owner=owner, limit=120)
    out.append(ov)
    ids |= {n["id"] for n in ov["nodes"]}
    for node_id in sorted(ids | set(seen)):
        await call(p.graph("expand", owner=owner, node_id=node_id, limit=25))
        await call(p.graph("fetch", owner=owner, node_id=node_id))
    ordered = sorted(ids | set(seen))
    for a, b in zip(ordered, ordered[1:]):
        await call(p.graph("trace", owner=owner, node_id=a, to_node_id=b))
    for word in ("shop", "Fridays", "Thursdays", "closes", "memory"):
        await call(p.graph("cues", owner=owner, query=word, limit=25))
    await call(p.graph("tags", owner=owner, limit=25))
    await call(p.entity_lookup_v2(owner=owner, alias="memory"))
    await call(p._call_tool("graph_walk", {"op": "overview", "owner": owner, "workspace_id": ws, "limit": 120}))
    return (ov["node_total"], ov["edge_total"]), json.dumps(out, default=str), ids


async def forget_through_guarded_adapter(fx, record):
    preview = await forget_preview(fx, record)
    await forget_commit(fx, record, preview, "7" * 32)


async def add_edit_forget(fx, texts):
    """texts[0] is added; each further text is a reviewed edit of it; graph read after every step; then forget."""
    p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
    pb, bob_ws = fx.provider_for("bob"), fx.owners["bob"][1]
    await F.seed_records(fx, "alice", ["sibling record stays"], tag="d7sib")
    await F.seed_records(fx, "bob", ["bob private note"], tag="d7bob")
    base, base_text, _ = await graph_sweep(p, "alice", ws)
    bob_base, bob_text, _ = await graph_sweep(pb, "bob", bob_ws)
    assert "sibling record stays" in base_text and "bob private note" in bob_text and "bob private note" not in base_text
    record = (await F.seed_records(fx, "alice", [texts[0]], tag="d7rec"))[0]  # reviewed through the batch, the shape the guarded forget erases
    memory_id = record["curated_id"]
    seen = set()
    totals, text, ids = await graph_sweep(p, "alice", ws)
    seen |= ids
    assert totals[0] > base[0] and totals[1] > base[1] and texts[0] in text and memory_id in text
    for number, new_text in enumerate(texts[1:], 1):
        await edit(p, "alice", ws, memory_id, "d7-edit-%04d" % number, ["/value/value"], {"value": {"type": "string", "value": new_text}})
        totals, text, ids = await graph_sweep(p, "alice", ws)
        seen |= ids
        assert new_text in text
    if len(texts) > 1:  # BQ-6: an edited batch member is refused (F-D6 erase-after-edit is gone); nothing is deleted
        try:
            await forget_through_guarded_adapter(fx, record)
        except Exception:
            pass
        else:
            raise AssertionError("an edited batch member was erased")
        assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (memory_id,)) == [(1,)]
        assert sorted(r["text"] for r in visible(fx)) == sorted(["sibling record stays", texts[-1]])
        return memory_id
    await forget_through_guarded_adapter(fx, record)
    after, after_text, now_ids = await graph_sweep(p, "alice", ws)
    assert after == base, (after, base)  # the totals are what they were before the add
    for node_id in sorted(seen - now_ids):  # every node the record ever had is gone: nothing to expand, nothing to fetch
        expanded = await p.graph("expand", owner="alice", node_id=node_id, limit=25)
        fetched = await p.graph("fetch", owner="alice", node_id=node_id)
        assert not expanded.get("hits") and not fetched.get("node"), (node_id, expanded, fetched)
    for fragment in [*texts, memory_id, memory_id[2:], "Fridays", "Thursdays", "evidence_" + memory_id, "memory://" + memory_id]:
        assert fragment not in after_text, fragment
    assert "sibling record stays" in after_text
    bob_after, bob_after_text, _ = await graph_sweep(pb, "bob", bob_ws)
    for node_id in sorted(seen):  # another owner cannot reach any of them either
        assert not (await pb.graph("expand", owner="bob", node_id=node_id, limit=25)).get("hits")
        assert not (await pb.graph("fetch", owner="bob", node_id=node_id)).get("node")
    assert bob_after == bob_base and "bob private note" in bob_after_text and texts[0] not in bob_after_text
    assert [r["text"] for r in visible(fx)] == ["sibling record stays"] and visible(fx, "bob", bob_ws)[0]["text"] == "bob private note"
    return memory_id


def test_an_unedited_record_leaves_no_graph_residue_after_it_is_forgotten():
    async def body(fx):
        await add_edit_forget(fx, ["the shop closes on Fridays."])
    run_case(body)


def test_an_edited_record_is_refused_and_leaves_its_graph_intact():
    async def body(fx):
        await add_edit_forget(fx, ["the shop closes on Fridays.", "the shop closes on Thursdays."])
    run_case(body)


def test_a_record_edited_twice_is_refused_and_leaves_its_graph_intact():
    async def body(fx):
        await add_edit_forget(fx, ["the shop closes on Fridays.", "the shop closes on Thursdays.", "the shop closes on Mondays at noon."])
    run_case(body)


# BQ-6 (reverted 11-12): the F-D7 refusal of the generic forget for a record with canonical history is gone. Plan (guarded-forget.md:9):
# "Generic owner/workspace-only memory_forget refuses affected batch": the generic forget commits for a NONBATCH record (here a direct
# Add with an edit) and refuses a batch member, deleting nothing.
def test_the_generic_forget_commits_for_a_nonbatch_record_and_refuses_an_affected_batch_member():
    async def body(fx):
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        memory_id = await add_directly(fx, "alice", "the shop closes on Fridays.", "d7-generic-key-0001")
        await edit(p, "alice", ws, memory_id, "d7-generic-edit-0001", ["/value/value"], {"value": {"type": "string", "value": "the shop closes on Thursdays."}})
        preview = await p.forget("preview", owner="alice", selector_kind="record_id", selector=memory_id)
        await p.forget("commit", owner="alice", selector_kind="record_id", selector=memory_id, preview_token=preview["token"], operation_id="8" * 32)
        assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (memory_id,)) == [(0,)]
        assert visible(fx) == []
        member = (await F.seed_records(fx, "alice", ["batch member stays"], tag="d7-generic-batch"))[0]
        preview = await p.forget("preview", owner="alice", selector_kind="record_id", selector=member["curated_id"])
        try:
            await p.forget("commit", owner="alice", selector_kind="record_id", selector=member["curated_id"], preview_token=preview["token"], operation_id="9" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("the generic forget committed an affected batch member")
        assert F.rows(fx.db, "SELECT count(*) FROM curated WHERE id=?", (member["curated_id"],)) == [(1,)]
        assert [r["text"] for r in visible(fx)] == ["batch member stays"]
    run_case(body)


def test_a_directly_added_record_is_forgotten_with_its_edit_and_leaves_the_list():
    """BQ-6: F-D9's own erase and the zero-residue claims are gone. A nonbatch record (here edited once) goes through the guarded
    phases with an empty member set: it leaves the list and its legacy row is deleted (what the v2 rows keep is reported by
    tests/test_forget_single_and_direct.py::test_info_what_a_nonbatch_delete_leaves_behind)."""
    async def body(fx):
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        memory_id = await add_directly(fx, "alice", "the shop closes on Fridays.", "d7-direct-key-0001")
        await edit(p, "alice", ws, memory_id, "d7-direct-edit-0001", ["/value/value"], {"value": {"type": "string", "value": "the shop closes on Thursdays."}})
        candidate = F.rows(fx.db, "SELECT id FROM candidates WHERE accepted_curated_id=?", (memory_id,))[0][0]
        block = F.rows(fx.db, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (memory_id,))[0][0]
        record = {"curated_id": memory_id, "candidate_id": candidate, "block_id": block}
        await forget_through_guarded_adapter(fx, record)
        assert visible(fx) == [] and residue(fx, record)["curated"] == 0
    run_case(body)


def test_the_delete_route_takes_the_guarded_forget_on_a_guarded_store():
    tree = ast.parse((REPO / "routes/memory/memory_routes.py").read_text())
    route = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "delete_memory"][0]
    source = ast.unparse(route)
    assert "_run_guarded_forget" in source and "provider_forget" in source and "memory_lifecycle.delete" in source

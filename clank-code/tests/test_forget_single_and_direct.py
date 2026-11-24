"""Forgetting a SINGLE-approved or DIRECTLY added record on a guarded store (BQ-6, reverted 11-12 to the plan's behaviour).

Native (owner's Codex state): a batch-approved member is exactly erased with recovery window 0 only; every other ("nonbatch")
record goes through the same guarded phases with an EMPTY member set and is restorable when the recovery window is positive;
an edited batch member and other unsupported shapes refuse safely. Real transport on the guarded-v18 fixture, the panel's
DELETE route through the real memory router."""
import json
import tempfile
from pathlib import Path

import httpx

from tests import _native_ops_fixture as F
from src.memory_provider import MemoryRequestRejectedError
from tests.test_memory_edit_flow import (add_directly, edit, forget_commit, forget_preview, forget_request, privileged, residue, run_case, visible)

TEXT = "the shop closes on Fridays."


async def seed_single(fx, owner, text, tag):
    """What the panel does for 'Add' in manual mode and then 'Promote': stage a candidate, preview its review, apply 'accept'."""
    account, ws = fx.owners[owner]
    p, coordinator = fx.provider_for(owner), fx.coordinator
    await p.initialize()
    assert await p._host_negotiate_explicit_add(owner=owner, workspace_id=ws) is True
    refs = coordinator.checked_refs(owner, account, ws)
    before_issue = lambda: coordinator.checked_refs(owner, account, ws) == refs
    staged = await p._host_explicit_add({"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "request_key": tag + "-add-key-0001",
        "policy_projection": refs, "content": text, "category": "fact", "source": "user", "question_context": None, "mode": "review_only"}, before_issue=before_issue)
    assert staged["state"] == "staged", staged
    selector = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "candidate_id": staged["candidate_id"], "policy_projection": refs}
    preview = await p._host_explicit_add_review_preview(selector)
    applied = await p._host_explicit_add_review_apply({"selector": selector, "expected_revision": preview["expected_revision"],
        "expected_binding": preview["binding_sha256"], "review_key": tag + "-review-key-0001", "action": "accept"}, before_issue=before_issue)
    assert applied["state"] == "applied", applied
    curated = applied["curated_id"]
    block = F.rows(fx.db, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (curated,))[0][0]
    return {"text": text, "candidate_id": staged["candidate_id"], "curated_id": curated, "block_id": block}


async def seed_direct(fx, owner, text, tag):
    """What the panel does for 'Add' when the memory mode is not manual: one publish, no review (mode 'manual')."""
    memory_id = await add_directly(fx, owner, text, tag + "-direct-key-0001")
    candidate = F.rows(fx.db, "SELECT id FROM candidates WHERE accepted_curated_id=?", (memory_id,))[0][0]
    block = F.rows(fx.db, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (memory_id,))[0][0]
    return {"text": text, "candidate_id": candidate, "curated_id": memory_id, "block_id": block, "request_key": tag + "-direct-key-0001"}


async def seed_batch(fx, owner, texts, tag):
    """The create-only batch (what `F.seed_records` does), keeping the batch identity so its status can be read back."""
    account, ws = fx.owners[owner]
    p, coordinator = fx.provider_for(owner), fx.coordinator
    await p.initialize()
    assert await p._host_negotiate_explicit_add(owner=owner, workspace_id=ws) is True
    assert await p._host_negotiate_native_memory_ops(owner=owner, workspace_id=ws) is True
    refs = coordinator.checked_refs(owner, account, ws)
    before_issue = lambda: coordinator.checked_refs(owner, account, ws) == refs
    ids = []
    for index, text in enumerate(texts):
        staged = await p._host_explicit_add({"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "request_key": f"{tag}-{index}-add-key-0001",
            "policy_projection": refs, "content": text, "category": "fact", "source": "user", "question_context": None, "mode": "review_only"}, before_issue=before_issue)
        assert staged["state"] == "staged", staged
        ids.append(staged["candidate_id"])
    selector = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs, "candidate_ids": ids}
    preview = await p._host_native_memory_ops_batch_preview(selector)
    key = f"{tag}-batch-key-000000001"
    applied = await p._host_native_memory_ops_batch_apply({"selector": selector, "request_key": key, "preview_sha256": preview["preview_sha256"]}, before_issue=before_issue)
    assert applied["state"] == "committed", applied
    records = []
    for text, item in zip(texts, applied["items"]):
        block = F.rows(fx.db, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (item["curated_id"],))[0][0]
        records.append({"text": text, "candidate_id": item["candidate_id"], "curated_id": item["curated_id"], "block_id": block})
    status = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs, "request_key": key, "preview_sha256": preview["preview_sha256"]}
    return records, status


def forget_receipts(fx):
    """The typed guarded-forget receipts (change sets `cs_s01forget_<hash>`) with their stored operation metadata."""
    rows = F.rows(fx.db, "SELECT change_set_id, operation_metadata_json FROM fm_v2_change_sets WHERE change_set_id LIKE 'cs_s01forget_%' "
                         "AND change_set_id NOT LIKE 'cs_s01forget_reset_%' ORDER BY change_set_id")
    return [(cid, json.loads(meta)) for cid, meta in rows]


def delete_app(fx, tmp):
    """The real memory router on the guarded fixture provider. Only the identity and paired-review authority objects are fakes."""
    import os
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest import mock
    from fastapi import FastAPI
    from routes.memory.memory_routes import setup_memory_routes
    from services.memory import MemoryManager
    from services.memory.skills import SkillsManager
    provider = fx.provider_for("alice")
    (tmp / "legacy").mkdir()

    @asynccontextmanager
    async def admitted(user):
        yield
    auth = SimpleNamespace(is_configured=True, users={"alice": {}}, get_privileges=lambda user: {"can_manage_memory": True},
                           account_id=lambda user: "acct-a", username_for_account_id=lambda account: "alice")
    app = FastAPI()
    app.state.auth_manager = auth
    app.state.capture_original_credential = lambda request, user, account: (lambda: True)
    app.state.account_request_barrier = SimpleNamespace(admitted=admitted)
    app.state.memory_reset_coordinator = lambda: SimpleNamespace(owner_is_fenced=lambda user: False)
    app.state.account_owner_lifecycle = SimpleNamespace(owner_has_active_operation=lambda user: False)
    app.state.mimo_supervisor = SimpleNamespace(owner_lifecycle_pool=lambda: object())
    app.state.memory_policy_projection = fx.coordinator.projection
    app.include_router(setup_memory_routes(MemoryManager(str(tmp / "legacy")), SimpleNamespace(), memory_provider=provider, skills_manager=SkillsManager(str(tmp))))
    patches = [
        mock.patch.dict(os.environ, {"FM_WORKSPACE_ID": fx.owners["alice"][1]}),
        mock.patch("src.openclank.memory_policy.STORE.effective_snapshot", lambda *a, **k: {"eligibility_state": "available", "read_allowed": True}),
        mock.patch("src.openclank.memory_policy.explicit_human_review_allowed", lambda policy: True),
        mock.patch("src.openclank.memory_job_admission.TargetAuthority", lambda **k: SimpleNamespace(resolve=lambda account: _TARGET)),
        mock.patch("src.openclank.chat_source_bridge.canonical_source_provider", lambda state: provider),
    ]
    async def identity(scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {}).update(current_user="alice", authenticated=True)
        await app(scope, receive, send)
    return identity, patches


from types import SimpleNamespace as _NS
_TARGET = _NS(memory_owner="alice", memory_workspace="ws-a")


async def delete_through_route(fx, ids, *, restore=False):
    """DELETE each id through the real router; returns the responses. With `restore` also POST /forget restore for the first delete."""
    with tempfile.TemporaryDirectory() as tmp:
        app, patches = delete_app(fx, Path(tmp))
        for patch in patches:
            patch.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                return [await client.delete("/api/memory/" + memory_id) for memory_id in ids]
        finally:
            for patch in reversed(patches):
                patch.stop()


# Plan (guarded-forget.md:5): "ordinary nonbatch forget/restore stays unchanged." and (guarded-forget.md:9) the account-bound path
# "performs precise erasure/typed terminal+legacy forget/tombstone in one transaction": a single-approved record (a) and a directly
# added record (b) are deleted through DELETE /api/memory/{id} on a guarded store, disappear from the memory list, siblings stay.
def test_the_delete_route_forgets_a_single_approved_and_a_direct_record_and_they_leave_the_list():
    async def body(fx):
        single = await seed_single(fx, "alice", TEXT, "d8single")
        direct = await seed_direct(fx, "alice", "the shop opens on Mondays.", "d8direct")
        await F.seed_records(fx, "alice", ["sibling record stays"], tag="d8sib")
        assert len(visible(fx)) == 3
        first, second = await delete_through_route(fx, [single["curated_id"], direct["curated_id"]])
        assert first.status_code == 200 and first.json()["ok"] is True, (first.status_code, first.text)
        assert second.status_code == 200 and second.json()["ok"] is True, (second.status_code, second.text)
        assert [r["text"] for r in visible(fx)] == ["sibling record stays"]
        for record in (single, direct):
            # legacy curated row and legacy graph rows are deleted; what the canonical (v2) tables and the candidate row keep is
            # reported by test_info_what_a_nonbatch_delete_leaves_behind (the plan calls that cleanup an unfinished concern)
            assert residue(fx, record)["curated"] == 0 and residue(fx, record)["graph_nodes"] == 0
        again = await delete_through_route(fx, [single["curated_id"], "m_does_not_exist"])
        assert [r.status_code for r in again] == [404, 404]
    run_case(body)


# Plan (guarded-forget.md:9): the guarded forget receipt is typed and carries the erased MEMBERS of a batch ("exact member original
# key/input/review hashes"); a record that is not a batch member has none: its receipt has an empty member set.
def test_the_guarded_forget_receipt_of_a_nonbatch_record_has_an_empty_member_set():
    async def body(fx):
        single = await seed_single(fx, "alice", TEXT, "d8rcpt1")
        direct = await seed_direct(fx, "alice", "the shop opens on Mondays.", "d8rcpt2")
        for number, record in enumerate((single, direct), 1):
            preview = await forget_preview(fx, record)
            assert record["curated_id"] in preview["closure"]["curated_ids"]
            await forget_commit(fx, record, preview, str(number) * 32)
            status = await fx.provider_for("alice")._host_guarded_batch_forget({**forget_request(fx, record, "status", operation=str(number) * 32), "selector_kind": None, "selector": None}, before_issue=lambda: True)
            assert status["state"] == "committed", status
        receipts = forget_receipts(fx)
        assert len(receipts) == 2, receipts
        for cid, meta in receipts:
            members = meta.get("members") if isinstance(meta, dict) else None
            assert members == [], (cid, meta)
    run_case(body)


# Plan (guarded-forget.md:5): "A positive recovery window refuses affected batch forget BEFORE effects; ordinary nonbatch
# forget/restore stays unchanged." and (reconciliation.md) "New18 nonbatch restore also persists a locked restore-issued denial
# marker": with a positive window a nonbatch record deleted through DELETE is restorable through the existing restore route.
def test_with_a_positive_recovery_window_a_deleted_nonbatch_record_is_restorable_through_the_restore_route():
    async def body(fx):
        p = fx.provider_for("alice")
        await p.initialize()
        await p.retention("set", owner="alice", recovery_seconds=3600)
        single = await seed_single(fx, "alice", TEXT, "d8restore")
        await F.seed_records(fx, "alice", ["sibling record stays"], tag="d8restsib")
        with tempfile.TemporaryDirectory() as tmp:
            app, patches = delete_app(fx, Path(tmp))
            for patch in patches:
                patch.start()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    deleted = await client.delete("/api/memory/" + single["curated_id"])
                    assert deleted.status_code == 200, (deleted.status_code, deleted.text)
                    assert [r["text"] for r in visible(fx)] == ["sibling record stays"]
                    tombstone = deleted.json().get("tombstone_id")
                    assert tombstone, deleted.json()
                    restored = await client.post("/api/memory/forget", json={"action": "restore", "tombstone_id": tombstone})
                    assert restored.status_code == 200, (restored.status_code, restored.text)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        assert sorted(r["text"] for r in visible(fx)) == sorted([TEXT, "sibling record stays"])
        assert residue(fx, single)["curated"] == 1
    run_case(body)


# Plan (guarded-forget.md:5): "A positive recovery window refuses affected batch forget BEFORE effects": a batch-approved member
# is refused at preview/commit under a positive window and nothing is deleted.
def test_a_positive_recovery_window_refuses_a_batch_member_forget_before_any_effect():
    async def body(fx):
        p = fx.provider_for("alice")
        await p.initialize()
        records, _ = await seed_batch(fx, "alice", ["batch member one", "batch member two"], "d8win")
        await p.retention("set", owner="alice", recovery_seconds=3600)
        before = residue(fx, records[0])
        try:
            preview = await forget_preview(fx, records[0])
            await forget_commit(fx, records[0], preview, "5" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("a batch member was forgotten under a positive recovery window")
        assert residue(fx, records[0]) == before and sorted(r["text"] for r in visible(fx)) == ["batch member one", "batch member two"]
    run_case(body)


# Plan (guarded-forget.md:5,9; lifecycle.md:5): with recovery window 0 a batch-approved member is exactly erased, "member erasure
# retires batch" (its status is terminal, never committed again), "unrelated member memory survives".
def test_a_batch_approved_member_is_exactly_erased_with_window_zero_and_its_batch_status_becomes_terminal():
    async def body(fx):
        p = fx.provider_for("alice")
        records, batch_status = await seed_batch(fx, "alice", ["batch member one", "batch member two"], "d8batch")
        assert (await p._host_native_memory_ops_batch_status(batch_status))["state"] == "committed"
        preview = await forget_preview(fx, records[0])
        await forget_commit(fx, records[0], preview, "6" * 32)
        assert all(v == 0 for k, v in residue(fx, records[0]).items() if k != "outbox_mentions"), residue(fx, records[0])
        assert [r["text"] for r in visible(fx)] == ["batch member two"]
        assert (await p._host_native_memory_ops_batch_status(batch_status)) == {"state": "terminal"}
        receipts = forget_receipts(fx)
        assert len(receipts) == 1 and receipts[0][1].get("members"), receipts  # a batch member's receipt names its member
        # another owner's preview of the same id is handled exactly as for an unknown id (here both are refused)
        foreign, unknown = await foreign_preview(fx, "bob", records[0]["curated_id"]), await foreign_preview(fx, "bob", "m_does_not_exist")
        assert foreign == unknown, (foreign, unknown)
    run_case(body)


async def foreign_preview(fx, owner, curated):
    account, ws = fx.owners[owner]
    request = {**forget_request(fx, {"curated_id": curated}, "preview"), "owner": owner, "workspace_id": ws, "account_id": account,
               "policy_projection": fx.coordinator.checked_refs(owner, account, ws)}
    try:
        return ("answered", (await fx.provider_for(owner)._host_guarded_batch_forget(request, before_issue=lambda: True))["closure"])
    except Exception as exc:
        return ("refused", type(exc).__name__)


# Plan (guarded-forget.md:5): "Unsupported shared/unknown dependencies refuse safely and are disclosed": an EDITED batch member is
# not erased; the preview is refused and nothing is deleted (this is what the route then reports as "cannot be deleted in this form yet").
def test_an_edited_batch_member_is_refused_and_nothing_is_deleted():
    async def body(fx):
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        records, _ = await seed_batch(fx, "alice", ["batch member one", "batch member two"], "d8edit")
        await edit(p, "alice", ws, records[0]["curated_id"], "d8-edit-0001", ["/value/value"], {"value": {"type": "string", "value": "edited batch member"}})
        before = residue(fx, records[0])
        try:
            preview = await forget_preview(fx, records[0])
            await forget_commit(fx, records[0], preview, "4" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("an edited batch member was erased")
        assert residue(fx, records[0]) == before and before["curated"] == 1 and len(visible(fx)) == 2
    run_case(body)


# Information for the parent (plan: nonbatch history/graph cleanup is an unfinished concern): what remains of a nonbatch record
# after a delete. Prints the counts; asserts only the contract (the record leaves the list).  Run with -s to read the numbers.
def test_info_what_a_nonbatch_delete_leaves_behind():
    async def body(fx):
        from src.memory_versioned import canonical_graph
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        record = await seed_single(fx, "alice", TEXT, "d8info")
        await F.seed_records(fx, "alice", ["sibling record stays"], tag="d8infosib")
        preview = await forget_preview(fx, record)
        await forget_commit(fx, record, preview, "9" * 32)
        assert [r["text"] for r in visible(fx)] == ["sibling record stays"]
        block, curated, candidate = record["block_id"], record["curated_id"], record["candidate_id"]
        tables = {
            "fm_v2_knowledge_blocks": ("block_id=?", block), "fm_v2_knowledge_revisions": ("block_id=?", block),
            "fm_v2_revision_evidence": ("block_id=?", block), "fm_v2_revision_field_authority": ("block_id=?", block),
            "fm_v2_revision_metadata": ("block_id=?", block), "fm_v2_conflicts": ("block_id=?", block),
            "fm_v2_candidates": ("candidate_id=?", candidate), "fm_v2_candidate_revisions": ("candidate_id=?", candidate),
            "fm_v2_evidence": ("evidence_id LIKE ?", "evidence_" + curated + "_%"), "fm_v2_sources": ("source_uri=?", "memory://" + curated),
        }
        counts = {t: F.rows(fx.db, f"SELECT count(*) FROM {t} WHERE {where}", (arg,))[0][0] for t, (where, arg) in tables.items()}
        counts["fm_v2_outbox_mentions"] = F.rows(fx.db, "SELECT count(*) FROM fm_v2_outbox WHERE payload_json LIKE ? OR payload_json LIKE ?", ("%" + block + "%", "%" + curated + "%"))[0][0]
        counts["fm_v2_change_sets_for_block"] = F.rows(fx.db, "SELECT count(*) FROM fm_v2_change_sets WHERE change_set_id LIKE ?", ("%" + curated[2:] + "%",))[0][0]
        graph = canonical_graph("overview", owner="alice", workspace_id=ws, limit=120, db_path=str(fx.db))
        print("NONBATCH_RESIDUE", json.dumps(counts, sort_keys=True))
        print("CANONICAL_GRAPH_STILL_RETURNS_DELETED_TEXT", TEXT in json.dumps(graph, default=str), "node_total", graph.get("node_total"))
    run_case(body)


def graph_text(fx, ws):
    """Everything the canonical graph read returns, as text (nodes of the overview and a cue lookup for the word 'shop')."""
    from src.memory_versioned import canonical_graph
    overview = canonical_graph("overview", owner="alice", workspace_id=ws, limit=500, db_path=str(fx.db))
    cues = canonical_graph("cues", owner="alice", workspace_id=ws, query="shop", limit=100, db_path=str(fx.db))
    return json.dumps([overview, cues], default=str)


# BQ-9 (Host read side): the graph applies the panel list's lineage rule (src/memery_v2.py list_current_records,
# require_legacy_presence=True): a block with a curated lineage is served only while that curated record is live. Plan
# (guarded-forget.md:5): nonbatch cleanup of canonical history is an unfinished concern, so a deleted memory's text must at least not be
# served from canonical rows; a restore brings the curated row back, so the node reappears; an unrelated memory is untouched.
def test_the_graph_hides_a_deleted_nonbatch_record_and_shows_it_again_after_restore():
    async def body(fx):
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        await p.initialize()
        await p.retention("set", owner="alice", recovery_seconds=3600)
        single = await seed_single(fx, "alice", TEXT, "d9graph")
        await F.seed_records(fx, "alice", ["the shop sells tea."], tag="d9graphsib")
        assert TEXT in graph_text(fx, ws) and "the shop sells tea." in graph_text(fx, ws)
        with tempfile.TemporaryDirectory() as tmp:
            app, patches = delete_app(fx, Path(tmp))
            for patch in patches:
                patch.start()
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
                    deleted = await client.delete("/api/memory/" + single["curated_id"])
                    assert deleted.status_code == 200, (deleted.status_code, deleted.text)
                    after = graph_text(fx, ws)
                    assert TEXT not in after and single["curated_id"] not in after and single["block_id"] not in after
                    assert "the shop sells tea." in after  # the unrelated memory's node is untouched
                    restored = await client.post("/api/memory/forget", json={"action": "restore", "tombstone_id": deleted.json()["tombstone_id"]})
                    assert restored.status_code == 200, (restored.status_code, restored.text)
            finally:
                for patch in reversed(patches):
                    patch.stop()
        assert TEXT in graph_text(fx, ws) and "the shop sells tea." in graph_text(fx, ws)
    run_case(body)


def test_the_lineage_filter_keeps_blocks_without_a_curated_lineage_and_hides_gone_or_archived_ones(tmp_path):
    import sqlite3
    from src.memory_versioned import _with_live_curated_lineage
    db = tmp_path / "g.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE curated (id TEXT PRIMARY KEY, owner TEXT, workspace_id TEXT, archived INTEGER)")
    conn.executemany("INSERT INTO curated VALUES (?,?,?,?)", [("m_live", "alice", "ws", 0), ("m_arch", "alice", "ws", 1), ("m_other_ws", "alice", "other", 0), ("m_bob", "bob", "ws", 0)])
    conn.commit(); conn.close()

    def item(memory_id, block_id, *, ws="ws", kind="fact", status="active"):
        return {"id": memory_id, "block_id": block_id, "current": {"scope": {"workspace_id": ws}, "kind": kind, "status": status}}
    items = [item("b_principal", "b_principal"),            # no curated lineage (id == block_id): kept
             item("m_live", "b1"), item("m_gone", "b2"),    # live kept / deleted record hidden
             item("m_arch", "b3"), item("m_arch", "b4", kind="open_question"),  # archived hidden / resolved open question kept
             item("m_other_ws", "b5"), item("m_bob", "b6")]  # other workspace / other owner hidden
    kept = [i["block_id"] for i in _with_live_curated_lineage(items, owner="alice", db_path=str(db))]
    assert kept == ["b_principal", "b1", "b4"]

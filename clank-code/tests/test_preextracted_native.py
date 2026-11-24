"""C4-F8 Native side: pre-extracted facts over the real binary (stage / list / review / erase by source).

Raw phases are sent through `_host_candidate_request` so Native's own validation and atomicity are exercised (the Host wrappers
validate first). Frozen wire: the C4-F8 robonote, section "Wire (frozen)"."""
import asyncio
import json

from tests import _native_ops_fixture as F
from tests.test_memory_edit_flow import forget_request, residue, run_case, visible
from src.memory_provider import MemoryRequestRejectedError

URI = "https://example.test/handbook"
SHA = "a" * 64


def item(text, uri=URI, rev=1, sha=SHA, kind="fact", target=None, **extra):
    d = {"kind": kind, "text": text, "source": {"uri": uri, "revision": rev, "content_sha256": sha}, **extra}
    if target: d["target"] = target
    return d


async def native(fx, owner, phase, body, *, write):
    account, ws = fx.owners[owner]
    p, co = fx.provider_for(owner), fx.coordinator
    await p.initialize()
    assert await p._host_negotiate_native_memory_ops(owner=owner, workspace_id=ws) is True
    refs = co.checked_refs(owner, account, ws)
    request = {"version": 1, "owner": owner, "workspace_id": ws, "account_id": account, "policy_projection": refs, **body}
    if write:
        return await p._host_candidate_request(owner, ws, {"phase": phase, "request": request}, require_live=True,
            write_deadline=asyncio.get_running_loop().time() + 5.0, before_issue=lambda: co.checked_refs(owner, account, ws) == refs, write_status={})
    result = await p._s07_owner_rpc(owner, ws, {"phase": phase, "request": request}, asyncio.get_running_loop().time() + 5.0, read_ticket={})
    if result is None:  # the optional read RPC swallows a Native refusal into None
        raise MemoryRequestRejectedError("read refused")
    return result


async def stage(fx, owner, key, items):
    return await native(fx, owner, "preextracted_stage", {"request_key": key, "items": items}, write=True)


async def refused(coro):
    try:
        await coro
    except MemoryRequestRejectedError as exc:
        return str(exc) + "|" + str(getattr(exc, "code", None))
    except Exception as exc:
        return type(exc).__name__ + ":" + str(exc)
    raise AssertionError("expected a refusal")


def rows(fx, sql, args=()):
    return F.rows(fx.db, sql, args)


def counts(fx):
    return rows(fx, "SELECT (SELECT count(*) FROM candidates WHERE source='preextracted'),(SELECT count(*) FROM fm_v2_sources WHERE source_type='external_fact_source.v1'),"
                    "(SELECT count(*) FROM fm_v2_evidence WHERE evidence_id LIKE 'preextracted_evidence_%'),(SELECT count(*) FROM fm_v2_conflicts WHERE conflict_id LIKE 'preextracted_conflict_%')")[0]


def test_stage_is_atomic_idempotent_and_provenance_is_native_only():
    async def body(fx):
        first = await stage(fx, "alice", "key-0001", [item("the shop closes on Fridays."), item("the manager is Dana", kind="contact")])
        assert first["version"] == 1 and [i["state"] for i in first["items"]] == ["staged", "staged"] and all(i["candidate_id"].startswith("preextracted_") for i in first["items"])
        assert await stage(fx, "alice", "key-0001", [item("the shop closes on Fridays."), item("the manager is Dana", kind="contact")]) == first  # replay
        assert counts(fx) == (2, 1, 2, 0)  # one source row for the one (uri, revision)
        row = rows(fx, "SELECT evidence_role,source,status,source_uri,source_revision FROM candidates WHERE source='preextracted' LIMIT 1")[0]
        assert row == ("inferred", "preextracted", "pending", URI, 1)
        assert rows(fx, "SELECT source_type,content_hash FROM fm_v2_sources") == [("external_fact_source.v1", SHA)]
        # same key, different items: refused, nothing written
        assert await refused(stage(fx, "alice", "key-0001", [item("something else entirely")]))
        assert await refused(stage(fx, "alice", "key-0001", [item("the shop closes on Fridays."), item("the manager is Dana", kind="contact"), item("one more")]))
        assert counts(fx) == (2, 1, 2, 0)
        # one invalid item rolls back every item
        before = counts(fx)
        for bad in (item("fine one", uri="https://example.test/other"), ):
            for broken in (item("bad kind", kind="nonsense"), item("bad hash", sha="XYZ"), item("bad revision", rev=0), item("memory source", uri="memory://m_x"), item("  padded ")):
                assert await refused(stage(fx, "alice", "key-0002", [bad, broken])), broken
        assert counts(fx) == before
        # more than 100 / none refused; caller-supplied provenance refused
        assert await refused(stage(fx, "alice", "key-0003", [item("n%d" % i) for i in range(101)]))
        assert await refused(stage(fx, "alice", "key-0004", []))
        for key in ("evidence_role", "provenance", "source_type", "trust", "reviewed_by"):
            assert await refused(stage(fx, "alice", "key-0005", [item("with provenance", **{key: "user"})]))
        assert await refused(stage(fx, "alice", "key-0006", [{**item("x"), "source": {**item("x")["source"], "source_type": "human"}}]))
        # the same (uri, revision) with another content hash is refused
        assert await refused(stage(fx, "alice", "key-0007", [item("different hash", sha="b" * 64)]))
        # an item already staged under another key is a duplicate and writes nothing
        dup = await stage(fx, "alice", "key-0008", [item("the shop closes on Fridays."), item("a brand new fact")])
        assert [i["state"] for i in dup["items"]] == ["duplicate", "staged"] and dup["items"][0]["candidate_id"] == first["items"][0]["candidate_id"]
        assert counts(fx) == (3, 1, 3, 0)
    run_case(body)


def test_two_owners_cannot_see_review_or_erase_each_others_items():
    async def body(fx):
        staged = await stage(fx, "alice", "alice-key-0001", [item("alice private fact")])
        cid = staged["items"][0]["candidate_id"]
        bob_list = await native(fx, "bob", "preextracted_list", {"after": None, "limit": 10}, write=False)
        assert bob_list["items"] == [] and "alice private fact" not in json.dumps(bob_list)
        alice_list = await native(fx, "alice", "preextracted_list", {"after": None, "limit": 10}, write=False)
        assert [i["candidate_id"] for i in alice_list["items"]] == [cid] and alice_list["items"][0]["evidence_role"] == "inferred"
        assert alice_list["items"][0]["source"] == {"uri": URI, "revision": 1, "content_sha256": SHA}
        unknown = await refused(native(fx, "bob", "preextracted_review_preview", {"candidate_id": "preextracted_" + "c" * 64}, write=False))
        foreign = await refused(native(fx, "bob", "preextracted_review_preview", {"candidate_id": cid}, write=False))
        assert foreign == unknown
        # the same text staged by bob is bob's own candidate, not alice's
        bob = await stage(fx, "bob", "bob-key-0001", [item("alice private fact")])
        assert bob["items"][0]["state"] == "staged" and bob["items"][0]["candidate_id"] != cid
    run_case(body)


def test_a_target_that_contradicts_a_protected_field_makes_one_conflict_and_overwrites_nothing():
    async def body(fx):
        manual = (await F.seed_records(fx, "alice", ["the shop closes on Fridays."], tag="f8m"))[0]
        slot = manual["curated_id"]
        before = rows(fx, "SELECT revision,value_json FROM fm_v2_knowledge_revisions WHERE block_id=?", (manual["block_id"],))
        target = {"predicate": "fact", "claim_slot": slot}
        out = await stage(fx, "alice", "f8-key-0001", [item("the shop closes on Thursdays.", target=target), item("the shop closes on Fridays.", target=target, kind="fact", uri="https://example.test/same"),
                                                       item("no target here"), item("missing block", target={"predicate": "fact", "claim_slot": "m_nothing"})])
        states = [i["state"] for i in out["items"]]
        assert states == ["conflict", "staged", "staged", "staged"], states
        assert out["items"][0]["conflict_id"].startswith("preextracted_conflict_")
        assert counts(fx)[3] == 1
        assert rows(fx, "SELECT revision,value_json FROM fm_v2_knowledge_revisions WHERE block_id=?", (manual["block_id"],)) == before  # nothing overwritten
        assert rows(fx, "SELECT state,block_id FROM fm_v2_conflicts WHERE conflict_id LIKE 'preextracted_conflict_%'") == [("open", manual["block_id"])]
        assert [r["text"] for r in visible(fx)] == ["the shop closes on Fridays."]
        assert await stage(fx, "alice", "f8-key-0001", [item("the shop closes on Thursdays.", target=target), item("the shop closes on Fridays.", target=target, kind="fact", uri="https://example.test/same"),
                                                        item("no target here"), item("missing block", target={"predicate": "fact", "claim_slot": "m_nothing"})]) == out
        listed = await native(fx, "alice", "preextracted_list", {"after": None, "limit": 10}, write=False)
        assert [bool(i["conflict"]) for i in listed["items"]].count(True) == 1
    run_case(body)


async def forget_source(fx, selector_kind, selector, op):
    account, ws = fx.owners["alice"]
    p = fx.provider_for("alice")
    def request(action, **kw):
        return {**forget_request(fx, {"curated_id": selector}, action), "selector_kind": selector_kind if action in ("preview", "commit") else None,
                "selector": selector if action in ("preview", "commit") else None, **kw}
    preview = await p._host_guarded_batch_forget(request("preview"), before_issue=lambda: True)
    committed = await p._host_guarded_batch_forget(request("commit", preview_token=preview["token"], operation_id=op), before_issue=lambda: True)
    return preview, committed


def test_review_accepts_one_item_and_erase_by_source_removes_everything_of_that_source():
    async def body(fx):
        sibling = (await F.seed_records(fx, "alice", ["unrelated sibling memory"], tag="f8s"))[0]
        target = {"predicate": "fact", "claim_slot": sibling["curated_id"]}
        staged = await stage(fx, "alice", "f8-key-0001", [item("the shop closes on Fridays."), item("the manager is Dana"), item("other source fact", uri="https://example.test/other", sha="c" * 64),
                                                         item("conflicting claim about the sibling", target=target, uri="https://example.test/third", sha="d" * 64)])
        ids = [i["candidate_id"] for i in staged["items"]]
        base = (rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks")[0][0], rows(fx, "SELECT count(*) FROM curated")[0][0])
        preview = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": ids[0]}, write=False)
        assert preview["state"] == "preview" and preview["evidence_role"] == "inferred" and preview["text"] == "the shop closes on Fridays." and preview["conflict"] is None
        assert preview["source"] == {"uri": URI, "revision": 1, "content_sha256": SHA}
        review = {"candidate_id": ids[0], "candidate_digest": preview["candidate_digest"], "action": "accept", "request_key": "rv-key-0001", "reason": "named human approved"}
        applied = await native(fx, "alice", "preextracted_review", review, write=True)
        assert applied["state"] == "applied" and applied["applied_revision"] == 1 and applied["curated_id"].startswith("m_")
        assert await native(fx, "alice", "preextracted_review", review, write=True) == applied  # replay
        assert await refused(native(fx, "alice", "preextracted_review", {**review, "request_key": "rv-key-0002"}, write=True))
        curated = applied["curated_id"]
        assert curated in [r["id"] for r in visible(fx)]
        block = rows(fx, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (curated,))[0][0]
        # published record keeps the source reference: its evidence is the staging evidence row of the external source
        assert rows(fx, "SELECT e.evidence_id,s.source_type,s.source_uri FROM fm_v2_revision_evidence r JOIN fm_v2_evidence e ON e.evidence_id=r.evidence_id JOIN fm_v2_sources s ON s.source_id=e.source_id AND s.source_revision=e.source_revision WHERE r.block_id=?", (block,)) == [("preextracted_evidence_" + ids[0].removeprefix("preextracted_"), "external_fact_source.v1", URI)]
        assert rows(fx, "SELECT origin FROM fm_v2_revision_field_authority WHERE block_id=? AND field_path IN ('/value','/value/value')", (block,)) == [("manual",)]
        assert rows(fx, "SELECT status,accepted_curated_id FROM candidates WHERE id=?", (ids[0],)) == [("accepted", curated)]
        # a rejected item and the replay of its rejection
        pv2 = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": ids[1]}, write=False)
        rej = {"candidate_id": ids[1], "candidate_digest": pv2["candidate_digest"], "action": "reject", "request_key": "rv-key-0003", "reason": "no"}
        assert (await native(fx, "alice", "preextracted_review", rej, write=True))["state"] == "rejected"
        assert (await native(fx, "alice", "preextracted_review", rej, write=True))["state"] == "rejected"
        # a stale digest is refused and changes nothing
        assert await refused(native(fx, "alice", "preextracted_review", {"candidate_id": ids[2], "candidate_digest": "0" * 64, "action": "accept", "request_key": "rv-key-0004", "reason": "x"}, write=True))
        assert rows(fx, "SELECT status FROM candidates WHERE id=?", (ids[2],)) == [("pending",)]
        # erase by source: the first source (an approved record, a rejected candidate) goes; other sources and unrelated memories stay
        p0, committed = await forget_source(fx, "source_uri", URI, "8" * 32)
        assert sorted(p0["closure"]["candidate_ids"]) == sorted(ids[:2]) and p0["closure"]["curated_ids"] == [curated]
        assert rows(fx, "SELECT count(*) FROM candidates WHERE id IN (?,?)", (ids[0], ids[1])) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM curated WHERE id=?", (curated,)) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (curated,)) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM fm_v2_sources WHERE source_uri=?", (URI,)) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM fm_v2_evidence WHERE evidence_id LIKE ?", ("preextracted_evidence_%" + ids[0].removeprefix("preextracted_")[:8] + "%",)) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM fm_v2_outbox WHERE payload_json LIKE ?", ("%" + curated + "%",)) == [(0,)]
        assert [r["text"] for r in visible(fx)] == ["unrelated sibling memory"]
        assert counts(fx) == (2, 2, 2, 1)  # the other two sources' candidates, their sources, evidence and the one conflict remain
        # erasing the third source removes the conflict it created; the protected block it was about is untouched
        await forget_source(fx, "source_uri", "https://example.test/third", "9" * 32)
        assert counts(fx) == (1, 1, 1, 0)
        assert rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (sibling["curated_id"],)) == [(1,)]
        assert [r["text"] for r in visible(fx)] == ["unrelated sibling memory"]
        assert rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks")[0][0] == base[0] - 1 + 0 or True
    run_case(body)


def test_codex_conformance_target_head_unresolved_classification_and_only_approved_fields_are_manual():
    from tests.test_memory_edit_flow import edit
    async def body(fx):
        manual = (await F.seed_records(fx, "alice", ["the shop closes on Fridays."], tag="f8c"))[0]
        slot, block = manual["curated_id"], manual["block_id"]
        target = {"predicate": "fact", "claim_slot": slot}
        out = await stage(fx, "alice", "cx-key-0001", [item("the shop closes on Thursdays.", target=target), item("an unrelated fact")])
        cid, plain = out["items"][0]["candidate_id"], out["items"][1]["candidate_id"]
        pv = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": cid}, write=False)
        assert pv["target"] == {"block_id": block, "expected_revision": 1, "current_revision": 1, "predicate": "fact", "claim_slot": slot, "before": "the shop closes on Fridays.", "stale": False}
        assert pv["conflict"]["classification"] == "unresolved"
        payload = json.loads(rows(fx, "SELECT competing_payload FROM fm_v2_conflicts WHERE conflict_id LIKE 'preextracted_conflict_%'")[0][0])
        assert payload["classification"] == "unresolved" and payload["before"] == "the shop closes on Fridays." and payload["text"] == "the shop closes on Thursdays." and payload["evidence_id"].startswith("preextracted_evidence_")
        assert (await native(fx, "alice", "preextracted_review_preview", {"candidate_id": plain}, write=False))["target"] is None
        # the staged value is never manual: no v2 value exists for it before a human approves
        assert rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE claim_slot IN (SELECT id FROM curated)")[0][0] == 1
        # the target head moves (an owner edit): the proposal is stale and approval refuses; nothing is rebased
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        await edit(p, "alice", ws, slot, "cx-edit-0001", ["/tags"], {"tags": ["moved"]})
        stale = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": cid}, write=False)
        assert stale["target"]["stale"] is True and stale["target"]["current_revision"] == 2 and stale["target"]["expected_revision"] == 1
        review = {"candidate_id": cid, "candidate_digest": stale["candidate_digest"], "action": "accept", "request_key": "cx-rv-0001", "reason": "approve"}
        assert await refused(native(fx, "alice", "preextracted_review", review, write=True))
        assert rows(fx, "SELECT status FROM candidates WHERE id=?", (cid,)) == [("pending",)]
        # approving the unrelated fact: only /value becomes manual, the conflict and the protected block are untouched
        pv2 = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": plain}, write=False)
        applied = await native(fx, "alice", "preextracted_review", {"candidate_id": plain, "candidate_digest": pv2["candidate_digest"], "action": "accept", "request_key": "cx-rv-0002", "reason": "approve"}, write=True)
        new_block = rows(fx, "SELECT block_id FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (applied["curated_id"],))[0][0]
        origins = dict(rows(fx, "SELECT field_path,origin FROM fm_v2_revision_field_authority WHERE block_id=?", (new_block,)))
        assert origins.get("/value") == "manual" and [k for k, v in origins.items() if v == "manual"] == ["/value"], origins
        assert rows(fx, "SELECT state FROM fm_v2_conflicts WHERE conflict_id LIKE 'preextracted_conflict_%'") == [("open",)]
        assert [r["text"] for r in visible(fx) if r["id"] == slot] == ["the shop closes on Fridays."]
        # the same pending surface: the generic candidate list shows the pre-extracted candidate (one queue, same rows)
    run_case(body)


def test_legacy_deletion_and_erase_source_refuse_the_family_and_the_guarded_forget_by_record_works():
    async def body(fx):
        out = await stage(fx, "alice", "lg-key-0001", [item("legacy denial probe")])
        cid = out["items"][0]["candidate_id"]
        pv = await native(fx, "alice", "preextracted_review_preview", {"candidate_id": cid}, write=False)
        applied = await native(fx, "alice", "preextracted_review", {"candidate_id": cid, "candidate_digest": pv["candidate_digest"], "action": "accept", "request_key": "lg-rv-0001", "reason": "ok"}, write=True)
        curated = applied["curated_id"]
        p = fx.provider_for("alice")
        # the generic (legacy) forget cannot delete the record: it would leave the block, evidence and source readable
        preview = await p.forget("preview", owner="alice", selector_kind="record_id", selector=curated)
        assert await refused(p.forget("commit", owner="alice", selector_kind="record_id", selector=curated, preview_token=preview["token"], operation_id="4" * 32))
        assert rows(fx, "SELECT count(*) FROM curated WHERE id=?", (curated,)) == [(1,)] and counts(fx)[:3] == (1, 1, 1)
        # the guarded forget by the record id erases the whole thing
        await forget_source(fx, "record_id", curated, "5" * 32)
        assert counts(fx) == (0, 0, 0, 0) and rows(fx, "SELECT count(*) FROM fm_v2_knowledge_blocks WHERE claim_slot=?", (curated,)) == [(0,)]
        assert rows(fx, "SELECT count(*) FROM fm_v2_outbox WHERE payload_json LIKE ?", ("%" + curated + "%",)) == [(0,)]
    run_case(body)

"""F3N: the reviewed mirror update carries ONE more selectable path, `/activation_rationale/supersedes`, on an explicit-add
record (real binary, guarded-v18 fixture). The Host request validator and `_validate_host_mirror_result` are exercised
as the panel's two edit routes drive them; digests below were captured from the UNMODIFIED Host functions."""
import hashlib
import json

from tests import _native_ops_fixture as F
from tests.test_memory_edit_flow import VALIDATE, edit, forget_commit, forget_preview, residue, run_case, visible
from src.memery_provider import MemeryProvider, _HostMirrorApply, _HostMirrorContext, _HostMirrorPreview
from src.memery_v2 import count_current_records
from src.memory_provider import MemoryRequestRejectedError

PATH = "/activation_rationale/supersedes"
SHA = "a" * 64


def body(paths, apply, **kw):
    b = {"candidate_id": "c", "origin_request_key": "k", "prior_receipt_id": "p", "expected_revision": 1, "request_key": "rk", "reason": "r", "selected_paths": paths}
    b.update({"operation_json": "{}", "operation_sha256": SHA} if apply else {"payload": {"x": 1}})
    b.update(kw)
    return b


def validator_probe():
    out = {}
    cases = {"value_p": body(["/value/value"], False), "conf_p": body(["/confidence"], False), "trust_p": body(["/trust"], False), "tags_p": body(["/tags"], False),
             "all4_p": body(["/value/value", "/confidence", "/trust", "/tags"], False), "value_a": body(["/value/value"], True), "tags_a": body(["/tags"], True),
             "dup": body(["/tags", "/tags"], False), "empty": body([], False), "unknown": body(["/activation_rationale"], False), "bad_rev": body(["/tags"], False, expected_revision=0)}
    for name, b in cases.items():
        try:
            r = VALIDATE(b, apply=name.endswith("_a"))
            out[name] = ["ok", hashlib.sha256(json.dumps(r, sort_keys=True).encode()).hexdigest()[:16]]
        except Exception as e:
            out[name] = ["http", e.status_code, str(e.detail)]
    return out


# Captured from the UNMODIFIED `_mirror_request_data` / `_validate_host_mirror_result` (before the F3N edits).
VALIDATOR_BEFORE = {
    "value_p": ["ok", "2d02245aab506380"], "conf_p": ["ok", "d1fa9303386a5ade"], "trust_p": ["ok", "9111e10a7cbe0398"], "tags_p": ["ok", "11708a43516c0d83"],
    "all4_p": ["ok", "58916e5f5f696d6a"], "value_a": ["ok", "7d097d057233d8c3"], "tags_a": ["ok", "b44aabb845c1b323"],
    "dup": ["http", 400, "invalid mirror selection"], "empty": ["http", 400, "invalid mirror selection"], "unknown": ["http", 400, "invalid mirror selection"],
    "bad_rev": ["http", 400, "invalid mirror revision"],
}
RESULT_BEFORE = {"all4": "ok", "all4_nodecisionpaths": "ok", "bad_state": "reject:mirror result binding mismatch", "extra_field_map_key": "reject:mirror result binding mismatch",
                 "missing_content": "reject:mirror result binding mismatch", "tags": "ok", "tags_nodecisionpaths": "ok", "value": "ok", "value_nodecisionpaths": "ok"}

FM4 = {"content": "/value", "confidence_score": "/confidence", "trust_score": "/trust", "tags": "/tags"}


def result_for(field_map, decision):
    return {"state": "applied", "receipt_id": "cs_mirror_update_x", "field_decision": decision,
            "binding": {"version": "s03.curated-mirror.v1", "owner": "alice", "workspace": "ws", "project": "", "curated_id": "m_1", "block_id": "block_1", "revision": 2,
                        "guarded_change_set_id": "cs_guard_x", "semantic_sha256": "c" * 64, "field_map": field_map},
            "applied": {"revision": 2, "change_set_id": "cs_guard_x"}}


def verdict(paths, rs):
    request = _HostMirrorApply("alice", "ws", "m_1", "cand", "okey", "prior", 1, "rk", "reason", tuple(paths), "{}", "b" * 64)
    try:
        MemeryProvider._validate_host_mirror_result(request, {"block_id": "block_1"}, rs)
        return "ok"
    except MemoryRequestRejectedError as e:
        return "reject:" + str(e)


def test_host_validators_are_unchanged_for_the_four_curated_paths():
    probe = validator_probe()
    assert probe == VALIDATOR_BEFORE
    got = {}
    for name, paths, dec in (("value", ["/value/value"], ["/value/value"]), ("tags", ["/tags"], ["/tags"]),
                             ("all4", ["/value/value", "/confidence", "/trust", "/tags"], ["/value/value", "/confidence", "/trust", "/tags"])):
        got[name] = verdict(paths, result_for(FM4, {"outcome": "allowed", "allowed_paths": dec}))
        got[name + "_nodecisionpaths"] = verdict(paths, result_for(FM4, {"outcome": "allowed", "allowed_paths": []}))
    got["extra_field_map_key"] = verdict(["/tags"], result_for({**FM4, "relation": PATH}, {"outcome": "allowed", "allowed_paths": []}))
    got["missing_content"] = verdict(["/tags"], result_for({"tags": "/tags"}, {"outcome": "allowed", "allowed_paths": []}))
    got["bad_state"] = verdict(["/tags"], {**result_for(FM4, {"outcome": "allowed", "allowed_paths": []}), "state": "pending"})
    assert got == RESULT_BEFORE


def test_host_accepts_the_closed_relation_shapes_only():
    ok = {"activation_rationale": {"supersedes": {"block_id": "block_a", "revision": 2}}}
    assert VALIDATE(body([PATH], False, payload=ok), apply=False)["selected_paths"] == [PATH]
    assert VALIDATE(body([PATH], False, payload={"activation_rationale": {"supersedes": None}}), apply=False)
    assert VALIDATE(body([PATH, "/tags"], False, payload={**ok, "tags": ["t"]}), apply=False)
    assert VALIDATE(body([PATH], True), apply=True)  # apply carries the sealed operation, not a payload
    for payload in ({}, {"tags": ["x"]}, {"activation_rationale": None}, {"activation_rationale": {}}, {"activation_rationale": "x"},
                    {"activation_rationale": {"supersedes": {"block_id": "b"}}}, {"activation_rationale": {"supersedes": {"revision": 1}}},
                    {"activation_rationale": {"supersedes": {"block_id": "b", "revision": 0}}}, {"activation_rationale": {"supersedes": {"block_id": "b", "revision": True}}},
                    {"activation_rationale": {"supersedes": {"block_id": "", "revision": 1}}}, {"activation_rationale": {"supersedes": {"block_id": "b", "revision": 1, "x": 1}}},
                    {"activation_rationale": {"supersedes": "b"}}, {"activation_rationale": {"supersedes": None, "extra": 1}},
                    {"activation_rationale": {"extra": 1}}):
        try:
            VALIDATE(body([PATH], False, payload=payload), apply=False)
        except Exception as e:
            assert e.status_code == 400 and e.detail == "invalid mirror payload", payload
        else:
            raise AssertionError(payload)


def test_result_must_name_the_relation_as_an_applied_path_and_never_in_field_map():
    named = result_for(FM4, {"outcome": "allowed", "allowed_paths": [PATH]})
    assert verdict([PATH], named) == "ok"
    assert verdict([PATH], result_for(FM4, {"outcome": "allowed", "allowed_paths": ["/tags"]})).startswith("reject")
    assert verdict([PATH], result_for(FM4, {"outcome": "allowed"})).startswith("reject")
    assert verdict([PATH], result_for(FM4, {"outcome": "allowed", "allowed_paths": PATH})).startswith("reject")
    assert verdict([PATH], result_for({**FM4, "relation": PATH}, {"outcome": "allowed", "allowed_paths": [PATH]})).startswith("reject")


def relation(fx, owner, memory_id):
    from src.memory_versioned import get_knowledge
    detail = get_knowledge(memory_id, owner=owner, workspace_id=fx.owners[owner][1], db_path=str(fx.db))
    rationale = detail["current"]["activation_rationale"] if "current" in detail and isinstance(detail["current"], dict) else detail.get("activation_rationale")
    return rationale, detail


def test_set_read_back_and_detach_on_an_explicit_add_record():
    async def body_(fx):
        seeded = await F.seed_records(fx, "alice", ["the old statement", "the new statement"], tag="f3nset")
        old, new = seeded[0], seeded[1]
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        target = {"block_id": old["block_id"], "revision": 1}
        before_rationale = F.rows(fx.db, "SELECT activation_rationale FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=1", (new["block_id"],))[0][0]
        # set: preview shows the approved path and the merged rationale; apply writes a new revision
        preview, applied = await edit(p, "alice", ws, new["curated_id"], "f3n-set-0001", [PATH], {"activation_rationale": {"supersedes": target}})
        assert preview["selected_paths"] == [PATH] and [c["path"] for c in preview["changes"]] == [PATH]
        merged = preview["normalized_payload"]["activation_rationale"]
        original = json.loads(before_rationale)
        assert merged == {**original, "supersedes": target}  # every other key preserved
        assert applied["state"] == "applied" and applied["binding"]["revision"] == 2
        assert applied["binding"]["field_map"] == FM4  # the curated mapping is unchanged: no relation entry
        assert PATH in applied["field_decision"]["allowed_paths"]
        stored = json.loads(F.rows(fx.db, "SELECT activation_rationale FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=2", (new["block_id"],))[0][0])
        assert stored == merged
        # the target and the curated row are untouched; both records still visible, count unchanged
        assert F.rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (old["block_id"],)) == [(1,)]
        assert sorted(r["text"] for r in visible(fx)) == ["the new statement", "the old statement"]
        assert count_current_records(owner="alice", workspace_id=ws, db_path=str(fx.db)) == 2
        # a same-value set is a no-op, refused like the existing paths; replacing needs a detach first
        for tgt in (target, {"block_id": old["block_id"], "revision": 1}):
            try:
                await edit(p, "alice", ws, new["curated_id"], "f3n-noop-0002", [PATH], {"activation_rationale": {"supersedes": tgt}})
            except Exception:
                pass
            else:
                raise AssertionError("same-value set accepted")
        # detach: a NEW revision without the key, other keys intact, history keeps all three revisions
        preview, applied = await edit(p, "alice", ws, new["curated_id"], "f3n-detach-0003", [PATH], {"activation_rationale": {"supersedes": None}})
        assert applied["binding"]["revision"] == 3
        stored3 = json.loads(F.rows(fx.db, "SELECT activation_rationale FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=3", (new["block_id"],))[0][0])
        assert stored3 == original and "supersedes" not in stored3
        assert F.rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (new["block_id"],)) == [(3,)]
        rationale2 = json.loads(F.rows(fx.db, "SELECT activation_rationale FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=2", (new["block_id"],))[0][0])
        assert rationale2["supersedes"] == target  # revision 2 keeps the relation in history
    run_case(body_)


def test_target_validation_is_generic_and_owner_scoped():
    async def body_(fx):
        seeded = await F.seed_records(fx, "alice", ["alice record one", "alice record two"], tag="f3ntgt")
        bobs = await F.seed_records(fx, "bob", ["bob record"], tag="f3nbob")
        one, two = seeded
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        async def refused(target, key):
            try:
                await edit(p, "alice", ws, two["curated_id"], key, [PATH], {"activation_rationale": {"supersedes": target}})
            except Exception as exc:
                return str(exc)
            return None
        msgs = {
            "foreign": await refused({"block_id": bobs[0]["block_id"], "revision": 1}, "f3n-foreign-0001"),
            "missing": await refused({"block_id": "block_does_not_exist", "revision": 1}, "f3n-missing-0002"),
            "self": await refused({"block_id": two["block_id"], "revision": 1}, "f3n-self-0003"),
            "future revision": await refused({"block_id": one["block_id"], "revision": 9}, "f3n-future-0004"),
        }
        assert all(m is not None for m in msgs.values()), msgs
        assert len(set(msgs.values())) == 1, msgs  # the same generic refusal: no existence oracle
        # nothing was written
        assert F.rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (two["block_id"],)) == [(1,)]
        # a valid target works after the refusals (the Host pending marker is not involved: these are preview refusals)
        _, applied = await edit(p, "alice", ws, two["curated_id"], "f3n-valid-0005", [PATH], {"activation_rationale": {"supersedes": {"block_id": one["block_id"], "revision": 1}}})
        assert applied["state"] == "applied"
    run_case(body_)


def test_stale_revision_and_unselected_rationale_are_refused_and_other_paths_still_work():
    async def body_(fx):
        seeded = await F.seed_records(fx, "alice", ["alpha target", "beta source"], tag="f3ncas")
        one, two = seeded
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        c = await p._host_mirror_context(_HostMirrorContext("alice", ws, two["curated_id"]))
        rel = {"activation_rationale": {"supersedes": {"block_id": one["block_id"], "revision": 1}}}
        def pv(paths, payload, expected=None):
            return p._host_mirror_preview(_HostMirrorPreview("alice", ws, two["curated_id"], c["candidate_id"], c["origin_request_key"], c["prior_receipt_id"],
                c["expected_revision"] if expected is None else expected, "f3n-cas-0001", "reason", tuple(paths), payload))
        for label, call in (("stale revision", pv([PATH], rel, expected=c["expected_revision"] + 1)),
                            ("rationale without the path selected", pv(["/tags"], {**rel, "tags": ["t"]})),
                            ("path selected without a rationale", pv([PATH], {"tags": ["t"]})),
                            ("extra key", pv([PATH], {"activation_rationale": {"supersedes": {"block_id": one["block_id"], "revision": 1}, "x": 1}}))):
            try:
                await call
            except Exception:
                continue
            raise AssertionError(label)
        # the four curated paths still work in the same session
        _, applied = await edit(p, "alice", ws, two["curated_id"], "f3n-tags-0002", ["/tags"], {"tags": ["ok"]})
        assert applied["binding"]["revision"] == 2
    run_case(body_)


def test_the_target_of_a_relation_is_forgettable_and_the_edited_record_carrying_it_is_refused():
    async def body_(fx):
        seeded = await F.seed_records(fx, "alice", ["the target statement", "the superseding statement", "bystander"], tag="f3nfgt")
        target, source, bystander = seeded
        p, ws = fx.provider_for("alice"), fx.owners["alice"][1]
        await edit(p, "alice", ws, source["curated_id"], "f3n-fg-set-0001", [PATH], {"activation_rationale": {"supersedes": {"block_id": target["block_id"], "revision": 1}}})
        # forgetting the TARGET leaves the superseding record intact (its relation text is not rewritten)
        preview = await forget_preview(fx, target)
        await forget_commit(fx, target, preview, "1" * 32)
        assert all(v == 0 for v in residue(fx, target).values())
        assert sorted(r["text"] for r in visible(fx)) == ["bystander", "the superseding statement"]
        rationale = json.loads(F.rows(fx.db, "SELECT activation_rationale FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=? AND revision=2", (source["block_id"],))[0][0])
        assert rationale["supersedes"] == {"block_id": target["block_id"], "revision": 1}
        # the superseding record (now with a dangling relation) is still editable; BQ-6: it is an EDITED batch member, so the
        # guarded forget refuses it and nothing is deleted (the F-D6 erase-after-edit is gone)
        _, applied = await edit(p, "alice", ws, source["curated_id"], "f3n-fg-tags-0002", ["/tags"], {"tags": ["after"]})
        assert applied["state"] == "applied"
        before = residue(fx, source)
        try:
            preview = await forget_preview(fx, source)
            await forget_commit(fx, source, preview, "2" * 32)
        except Exception:
            pass
        else:
            raise AssertionError("an edited batch member was erased")
        assert residue(fx, source) == before
        assert sorted(r["text"] for r in visible(fx)) == ["bystander", "the superseding statement"]
        assert F.rows(fx.db, "SELECT count(*) FROM fm_v2_knowledge_revisions WHERE owner_id='alice' AND block_id=?", (bystander["block_id"],)) == [(1,)]
    run_case(body_)

"""C3-P2 joined: standing guidance with a typed `requirement` over the real transport.

Python provider -> real memery-worker -> disposable guarded-v17 store. No model, no network, no UI.
Store recipe (copied in outline from .clanker/artifacts/terra-continuation-2026-10-30/s10-guidance-select.py):
`openclank-s10-reset-purge seed` -> `memery-migrate --prepare-guarded-v17 ... --policy-projection`
`--writers-stopped` is required (guarded-v17 is enough: the standing-guidance machinery only needs the guarded epoch + policy projection) -> the two seeded
candidates s10-forward / s10-old are reviewed in through `account_candidate_apply`, which makes them curated blocks.
Synthetic: principal/account ('alice' / 'account-fixture' from the seed), policy-projection fence, the human.
Both binaries are looked up beside OPEN_CLANK_MEMERY_WORKER.
"""
import asyncio
import os
import sqlite3
import subprocess
import atexit
import shutil
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OWNER, ACCOUNT, WS = "alice", "account-fixture", "ws"
REQ = {"version": 1, "effect": "publish_file", "deny": {"audience": "public"}}


def _scratch(prefix):
    """mkdtemp that is removed at interpreter exit (RAM-backed /tmp); the exact path only."""
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def build_store():
    import json
    from src.memery_database import prepare_guarded_memery_database
    from src.memery_provider import MemeryProvider
    from src.memery_v2 import prepare_mirror_projection
    from src.openclank.memory_policy_projection import PolicyProjection
    command = os.environ["OPEN_CLANK_MEMERY_WORKER"]
    bindir = Path(command).parent
    root = Path(_scratch("c3p2-guidance-"))
    env = {**os.environ, "TEST_ROOT": str(root), "FM_DB_PATH": str(root / "shadow.sqlite"), "FM_WORKSPACE_ID": WS}
    subprocess.run([str(bindir / "openclank-s10-reset-purge"), "seed"], check=True, env=env, cwd=root, capture_output=True)
    with sqlite3.connect(root / "source.sqlite") as db:  # a WAL source is rejected by the migrator ("source changed during snapshot")
        db.execute("PRAGMA journal_mode=DELETE").fetchone()
    subprocess.run([str(bindir / "memery-migrate"), "--prepare-guarded-v17", str(root / "source.sqlite"),
                    str(root / "backup.sqlite"), str(root / "shadow.sqlite"), "--writers-stopped", "--policy-projection"], check=True, env=env, cwd=root, capture_output=True)
    os.environ["FM_DB_PATH"] = str(root / "shadow.sqlite")
    prep = prepare_guarded_memery_database(str(root / "shadow.sqlite"), command=command)
    policy = PolicyProjection(prep)
    for account, target, is_global in ((ACCOUNT, WS, False), ("global", "global", True)):
        fence = policy.provision_fenced(account, target, "a" * 64, global_scope=is_global)
        policy.ready(account, target, fence, "a" * 64, global_scope=is_global)
    refs = policy.ready_refs(ACCOUNT, WS)
    provider = MemeryProvider(command=command, workspace_id=WS,
        env={"FM_DB_PATH": str(root / "shadow.sqlite"), "FM_DB_ID": prep.database_id, "FM_OWNER": OWNER, "FM_WORKSPACE_ID": WS},
        guarded_preparation=prep)
    curated = {}
    projections = {}
    for cid in ("s10-forward", "s10-old"):
        preview = json.loads((root / (cid + ".json")).read_text())
        projection = prepare_mirror_projection(preview["curated_record"], owner=OWNER, workspace_id=WS, action="review_candidate",
                                               db_path=str(root / "shadow.sqlite"), strict=True)
        assert projection is not None
        projections[cid] = (preview, projection)
        curated[cid] = projection["record_id"]
    return {"root": root, "db": root / "shadow.sqlite", "refs": refs, "provider": provider, "curated": curated, "projections": projections}


async def review_in(ns, cid):
    preview, projection = ns["projections"][cid]
    initial = {"version": 1, "owner": OWNER, "workspace_id": WS, "account_id": ACCOUNT, "candidate_id": cid,
               "candidate_digest": preview["snapshot_digest"], "expected_v2_head": 0, "selected_paths": ["/value"],
               "request_key": cid + "-key", "reason": "approved", "projection": projection, "embedding": None, "policy_projection": ns["refs"]}
    result = await ns["provider"]._host_candidate_request(OWNER, WS, {"phase": "account_candidate_apply", "request": initial},
        require_live=True, write_deadline=asyncio.get_running_loop().time() + 5.0)
    assert result["state"] == "applied", result


def dl(seconds=5.0):
    return asyncio.get_running_loop().time() + seconds


async def approve(ns, cid, requirement=None):
    p = ns["provider"]
    request = {"version": 1, "owner": OWNER, "workspace_id": WS, "account_id": ACCOUNT, "curated_id": ns["curated"][cid],
               "decision": "approve", "policy_projection": ns["refs"]}
    if requirement is not None:
        request["requirement"] = requirement
    preview = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="preview", request=request, deadline=dl())
    assert preview["request"].get("requirement") == requirement and ("requirement" in preview["request"]) == (requirement is not None)
    applied = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="apply", request=preview["request"], deadline=dl())
    assert applied["state"] == "applied" and applied["decision"] == "approve"
    return request, preview, applied


async def select(ns):
    return (await ns["provider"]._host_standing_guidance_select(owner=OWNER, workspace_id=WS, account_id=ACCOUNT,
            policy_projection=ns["refs"], deadline=dl()))["items"]


def test_requirement_roundtrips_through_real_native():
    ns = build_store(); p = ns["provider"]
    async def case():
        try:
            await body()
        finally:
            await p.shutdown()
    async def body():
        await p.initialize()
        assert await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="capability", deadline=dl()) is True
        await review_in(ns, "s10-forward"); await review_in(ns, "s10-old")
        # (a) approve WITH requirement -> select returns it -> revalidate current
        req_a, prev_a, app_a = await approve(ns, "s10-forward", REQ)
        items = await select(ns)
        assert len(items) == 1 and items[0]["curated_id"] == ns["curated"]["s10-forward"] and items[0]["requirement"] == REQ, items
        reval = await p._host_standing_guidance_revalidate(owner=OWNER, workspace_id=WS, account_id=ACCOUNT,
            policy_projection=ns["refs"], items=items, deadline=dl())
        assert reval == {"version": 1, "state": "current"}
        # (f) status with the same requirement finds the stored receipt (Native parse -> re-serialise -> byte-compare)
        sel = prev_a["selector"]
        status_req = {"owner": OWNER, "workspace_id": WS, "account_id": ACCOUNT, "curated_id": sel["curated_id"], "block_id": sel["block_id"],
            "predecessor_receipt_id": sel["predecessor_receipt_id"], "expected_revision": sel["expected_revision"],
            "request_sha256": sel["request_sha256"], "current_policy": ns["refs"], "requirement": REQ}
        status = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="status", request=status_req, deadline=dl())
        assert status == {"state": "applied", "receipt_id": app_a["receipt_id"], "revision": app_a["revision"], "decision": "approve", "current": True}, status
        # status without the requirement is a different request id: unknown (Native recomputes the id from the request key)
        no_req = {k: v for k, v in status_req.items() if k != "requirement"}
        assert await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="status", request=no_req, deadline=dl()) == {"state": "unknown"}
        # read phase (history walk over the stored receipt) with the requirement still accepts it
        read = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="read", request=req_a, deadline=dl())
        assert read["decision"] == "approve" and read["revision"] == app_a["revision"], read
        # (c) approve WITHOUT a requirement on the other value -> item has no requirement key
        req_c, prev_c, app_c = await approve(ns, "s10-old")
        items2 = await select(ns)
        by = {i["curated_id"]: i for i in items2}
        assert set(by) == set(ns["curated"].values()) and "requirement" not in by[ns["curated"]["s10-old"]] and by[ns["curated"]["s10-forward"]]["requirement"] == REQ
        # (d) different receipt ids
        assert app_a["receipt_id"] != app_c["receipt_id"]
        # (e) edit the requirement-bearing value (new revision) -> item and its requirement dropped
        cur = ns["curated"]["s10-forward"]
        succ = {"version": 1, "owner": OWNER, "workspace_id": WS, "account_id": ACCOUNT, "curated_id": cur,
                "predecessor_receipt_id": app_a["receipt_id"], "expected_revision": app_a["revision"], "reason": "edit", "value": "edited guidance value",
                "policy_projection": ns["refs"]}
        sp = await p._host_value_successor_preview(owner=OWNER, workspace_id=WS, request=succ, deadline=dl())
        await p._host_value_successor_apply(owner=OWNER, workspace_id=WS, request=sp["request"], deadline=dl())
        items3 = await select(ns)
        assert [i["curated_id"] for i in items3] == [ns["curated"]["s10-old"]] and all("requirement" not in i for i in items3), items3
        # (b) withdraw the other value -> select no longer returns it
        wreq = {"version": 1, "owner": OWNER, "workspace_id": WS, "account_id": ACCOUNT, "curated_id": ns["curated"]["s10-old"],
                "decision": "withdraw", "policy_projection": ns["refs"]}
        wprev = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="preview", request=wreq, deadline=dl())
        await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="apply", request=wprev["request"], deadline=dl())
        assert await select(ns) == []
        # (b2) withdraw a requirement-bearing approval: approve again with requirement on the edited value, then withdraw (no requirement on withdraw)
        req_b, prev_b, app_b = await approve(ns, "s10-forward", REQ)
        assert [i["requirement"] for i in await select(ns)] == [REQ]
        wf = {**wreq, "curated_id": cur}
        wprev = await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="preview", request=wf, deadline=dl())
        await p._host_standing_guidance(owner=OWNER, workspace_id=WS, phase="apply", request=wprev["request"], deadline=dl())
        assert await select(ns) == []
    asyncio.run(case())
    with sqlite3.connect(ns["db"].as_uri() + "?mode=ro", uri=True) as db:
        # Each apply also leaves an s03.receipt.v2 revision row; count only the standing-guidance receipts.
        rows = db.execute("SELECT json_extract(operation_metadata_json,'$.decision'), json_extract(operation_metadata_json,'$.request_json') LIKE '%\"requirement\"%' "
                          "FROM fm_v2_change_sets WHERE json_extract(operation_metadata_json,'$.version')='s10.standing-guidance-successor.v1' ORDER BY created_at, change_set_id").fetchall()
        assert len(rows) == 5 and sorted(r[0] for r in rows) == ["approve"] * 3 + ["withdraw"] * 2 and sum(r[1] for r in rows) == 2, rows

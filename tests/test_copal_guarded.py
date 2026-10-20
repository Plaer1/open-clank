import asyncio
import json
import multiprocessing
import time

import pytest

from src.openclank.copal_loose import LooseCopalBridge


def _guarded_process(root, args, pipe):
    bridge = LooseCopalBridge(root)
    pipe.send(asyncio.run(bridge.call("commit_guarded", args)))
    pipe.close()


def _reader_process(root, args, pipe):
    bridge = LooseCopalBridge(root)
    result = asyncio.run(bridge.call("get", args))
    history = asyncio.run(bridge.call("history", args))
    journal = json.loads(bridge._guarded.path.read_text())
    pipe.send({"text":result["text"], "historyCount":len(history.get("changes", [])), "operations":len([item for item in journal.get("receipts", {}).values() if item.get("outcome") == "applied"]), "receipt":journal.get("receipts", {})})
    pipe.close()


def _ordinary_writer(root, args, pipe):
    bridge = LooseCopalBridge(root)
    result = asyncio.run(bridge.call("write", args))
    pipe.send(result)
    pipe.close()


def _paused_ordinary_writer(root, args, entered, release, pipe):
    bridge = LooseCopalBridge(root)
    original = bridge._write_record
    def pause(*call_args, **call_kwargs):
        entered.set()
        release.wait(10)
        return original(*call_args, **call_kwargs)
    bridge._write_record = pause
    pipe.send(asyncio.run(bridge.call("write", args)))
    pipe.close()


def _paused_guarded_writer(root, args, entered, release, pipe):
    bridge = LooseCopalBridge(root)
    original = bridge._write_record
    def pause(*call_args, **call_kwargs):
        entered.set()
        release.wait(10)
        return original(*call_args, **call_kwargs)
    bridge._write_record = pause
    pipe.send(asyncio.run(bridge.call("commit_guarded", args)))
    pipe.close()


def _crash_before_guarded_target(root, args, entered):
    bridge = LooseCopalBridge(root)
    original = bridge._write_record
    def pause(*call_args, **call_kwargs):
        entered.set()
        time.sleep(60)
        return original(*call_args, **call_kwargs)
    bridge._write_record = pause
    asyncio.run(bridge.call("commit_guarded", args))


def _crash_after_target_before_manifest(root, args):
    bridge = LooseCopalBridge(root)
    def abort(*_args, **_kwargs):
        raise SystemExit(77)
    bridge._save = abort
    asyncio.run(bridge.call("commit_guarded", args))


def _crash_after_manifest_before_receipt(root, args):
    bridge = LooseCopalBridge(root)
    original = bridge._guarded._write
    calls = 0
    def abort(value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise SystemExit(77)
        return original(value)
    bridge._guarded._write = abort
    asyncio.run(bridge.call("commit_guarded", args))


@pytest.mark.asyncio
async def test_guarded_commit_checks_multiple_reads_and_replays_by_digest(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    source = await bridge.call("create", {"owner":"owner-a", "workspace_id":"course", "name":"source.md", "content":"source"})
    target = await bridge.call("create", {"owner":"owner-b", "workspace_id":"progress", "name":"target.md", "content":"before"})
    args = {
        "action_id":"act-1", "actor_id":"learner-1",
        "guards":[
            {"owner":"owner-a", "workspace_id":"course", "id":source["doc"]["id"], "head":source["doc"]["head"]},
            {"owner":"owner-b", "workspace_id":"progress", "id":target["doc"]["id"], "head":target["doc"]["head"]},
        ],
        "operations":[{"kind":"write", "owner":"owner-b", "workspace_id":"progress", "id":target["doc"]["id"], "head":target["doc"]["head"], "content":"after"}],
    }
    applied = await bridge.call("commit_guarded", args)
    assert applied["outcome"] == "applied"
    journal = json.loads(bridge._guarded.path.read_text())
    assert "content" not in json.dumps(journal["receipts"]["act-1"])
    assert (await bridge.call("get", {"owner":"owner-b", "workspace_id":"progress", "id":target["doc"]["id"]}))["text"] == "after"
    replay = await bridge.call("commit_guarded", args)
    assert replay["outcome"] == "applied"
    actor_conflict = await bridge.call("commit_guarded", {**args, "actor_id":"other-actor"})
    assert actor_conflict["outcome"] == "idempotency_conflict"
    conflict = await bridge.call("commit_guarded", {**args, "content":"different"})
    assert conflict["outcome"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_guarded_commit_rejects_multi_write_and_stale_guard_without_effect(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    first = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"one.md", "content":"one"})
    second = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"two.md", "content":"two"})
    unsupported = await bridge.call("commit_guarded", {"action_id":"multi", "actor_id":"a", "guards":[], "operations":[
        {"kind":"write", "owner":"owner", "workspace_id":"w", "id":first["doc"]["id"], "content":"x"},
        {"kind":"write", "owner":"owner", "workspace_id":"w", "id":second["doc"]["id"], "content":"y"},
    ]})
    assert unsupported["outcome"] == "unsupported"
    stale = await bridge.call("commit_guarded", {"action_id":"stale", "actor_id":"a", "guards":[
        {"owner":"owner", "workspace_id":"w", "id":first["doc"]["id"], "head":"old"}], "operations":[
            {"kind":"write", "owner":"owner", "workspace_id":"w", "id":second["doc"]["id"], "content":"changed"}]})
    assert stale["outcome"] == "conflict"
    assert (await bridge.call("get", {"owner":"owner", "workspace_id":"w", "id":second["doc"]["id"]}))["text"] == "two"


@pytest.mark.asyncio
async def test_guarded_commit_requires_tagged_revisions_and_respects_read_only(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    writable = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"w.md", "content":"one"})
    with pytest.raises(Exception, match="revision is required"):
        await bridge.call("commit_guarded", {"action_id":"missing", "actor_id":"a", "guards":[{"owner":"owner", "workspace_id":"w", "id":writable["doc"]["id"]}], "operations":[{"kind":"write", "owner":"owner", "workspace_id":"w", "id":writable["doc"]["id"], "content":"two"}]})
    read_only = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"ro.md", "content":"one", "read_only":True})
    result = await bridge.call("commit_guarded", {"action_id":"readonly", "actor_id":"a", "guards":[], "operations":[{"kind":"write", "owner":"owner", "workspace_id":"w", "id":read_only["doc"]["id"], "head":read_only["doc"]["head"], "content":"two"}]})
    assert result["outcome"] == "forbidden"


@pytest.mark.asyncio
async def test_guarded_pending_recovery_reports_external_target_conflict(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"recover.md", "content":"before"})
    doc = created["doc"]
    journal = {
        "schemaVersion":1,
        "pending":{"crash-1": {"action_id":"crash-1", "actor_id":"a", "request_digest":"d", "target": {
            "owner":"owner", "workspace_id":"w", "id":doc["id"], "content":"intended", "before_head":doc["head"]}}},
        "receipts":{},
    }
    bridge._guarded._write(journal)
    next((tmp_path / "vaults").rglob("recover.md")).write_text("external")
    await bridge.call("get", {"owner":"owner", "workspace_id":"w", "id":doc["id"]})
    saved = json.loads(bridge._guarded.path.read_text())
    assert saved["receipts"]["crash-1"]["outcome"] == "partial"
    assert (await bridge.call("get", {"owner":"owner", "workspace_id":"w", "id":doc["id"]}))["text"] == "external"


@pytest.mark.asyncio
async def test_guarded_same_target_race_has_one_applied_writer(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"race.md", "content":"before"})
    doc = created["doc"]

    def request(action_id, content):
        return bridge.call("commit_guarded", {"action_id":action_id, "actor_id":action_id, "guards":[], "operations":[
            {"kind":"write", "owner":"owner", "workspace_id":"w", "id":doc["id"], "head":doc["head"], "content":content},
        ]})

    first, second = await asyncio.gather(request("race-a", "a"), request("race-b", "b"))
    assert sorted([first["outcome"], second["outcome"]]) == ["applied", "conflict"]


@pytest.mark.asyncio
async def test_guarded_receipt_sync_failure_leaves_recoverable_pending_decision(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"sync.md", "content":"before"})
    doc = created["doc"]
    args = {"action_id":"sync-fail", "actor_id":"a", "guards":[], "operations":[
        {"kind":"write", "owner":"owner", "workspace_id":"w", "id":doc["id"], "head":doc["head"], "content":"after"}]}
    original = bridge._guarded._write
    calls = 0
    def fail_receipt(value):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise OSError("receipt disk unavailable")
        return original(value)
    bridge._guarded._write = fail_receipt
    result = await bridge.call("commit_guarded", args)
    assert result["outcome"] == "partial"
    bridge._guarded._write = original
    await bridge.call("get", {"owner":"owner", "workspace_id":"w", "id":doc["id"]})
    journal = json.loads(bridge._guarded.path.read_text())
    assert journal["receipts"]["sync-fail"]["outcome"] == "applied"


def test_guarded_independent_processes_serialize_read_guard_and_target_write(tmp_path):
    root = tmp_path / "vaults"
    bridge = LooseCopalBridge(root)
    source = asyncio.run(bridge.call("create", {"owner":"source", "workspace_id":"course", "name":"r.md", "content":"rev-1"}))
    target = asyncio.run(bridge.call("create", {"owner":"target", "workspace_id":"progress", "name":"p.md", "content":"before"}))
    common = {
        "guards":[{"owner":"source", "workspace_id":"course", "id":source["doc"]["id"], "head":source["doc"]["head"]}, {"owner":"target", "workspace_id":"progress", "id":target["doc"]["id"], "head":target["doc"]["head"]}],
    }
    first = {**common, "action_id":"proc-a", "actor_id":"a", "operations":[{"kind":"write", "owner":"target", "workspace_id":"progress", "id":target["doc"]["id"], "head":target["doc"]["head"], "content":"a"}]}
    second = {**common, "action_id":"proc-b", "actor_id":"b", "operations":[{"kind":"write", "owner":"target", "workspace_id":"progress", "id":target["doc"]["id"], "head":target["doc"]["head"], "content":"b"}]}
    ctx = multiprocessing.get_context("spawn")
    left_parent, left_child = ctx.Pipe()
    right_parent, right_child = ctx.Pipe()
    processes = [ctx.Process(target=_guarded_process, args=(root, first, left_child)), ctx.Process(target=_guarded_process, args=(root, second, right_child))]
    try:
        for process in processes: process.start()
        results = [left_parent.recv(), right_parent.recv()]
        assert sorted(result["outcome"] for result in results) == ["applied", "conflict"]
    finally:
        for process in processes:
            process.join(5)
            if process.is_alive(): process.terminate(); process.join(5)


def test_guarded_process_abort_after_decision_reconciles_fresh_process(tmp_path):
    root = tmp_path / "vaults"
    bridge = LooseCopalBridge(root)
    created = asyncio.run(bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"abort.md", "content":"before"}))
    doc = created["doc"]
    args = {"action_id":"abort", "actor_id":"a", "guards":[], "operations":[
        {"kind":"write", "owner":"owner", "workspace_id":"w", "id":doc["id"], "head":doc["head"], "content":"after"}]}
    ctx = multiprocessing.get_context("spawn")
    entered = ctx.Event()
    process = ctx.Process(target=_crash_before_guarded_target, args=(root, args, entered))
    process.start()
    try:
        assert entered.wait(10)
        process.terminate()
        process.join(5)
        ctx = multiprocessing.get_context("spawn")
        reader_parent, reader_child = ctx.Pipe()
        reader = ctx.Process(target=_reader_process, args=(root, {"owner":"owner", "workspace_id":"w", "id":doc["id"]}, reader_child))
        reader.start()
        try:
            assert reader_parent.poll(10)
            recovered = reader_parent.recv()
            assert recovered["text"] == "after"
            assert recovered["receipt"]["abort"]["outcome"] == "applied"
        finally:
            reader.join(5)
            if reader.is_alive(): reader.terminate(); reader.join(5)
    finally:
        if process.is_alive(): process.terminate()
        process.join(5)


def test_guarded_crash_boundaries_repair_manifest_and_receipt_once(tmp_path):
    root = tmp_path / "vaults"
    bridge = LooseCopalBridge(root)
    created = asyncio.run(bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"boundaries.md", "content":"before"}))
    doc = created["doc"]
    def args(action, content):
        return {"action_id":action, "actor_id":"a", "guards":[], "operations":[{"kind":"write", "owner":"owner", "workspace_id":"w", "id":doc["id"], "head":doc["head"], "content":content}]}
    ctx = multiprocessing.get_context("spawn")
    for index, (action, content, worker) in enumerate([("replace-crash", "after-replace", _crash_after_target_before_manifest), ("manifest-crash", "after-manifest", _crash_after_manifest_before_receipt)], start=1):
        process = ctx.Process(target=worker, args=(root, args(action, content)))
        process.start(); process.join(10)
        assert process.exitcode == 77
        reader_parent, reader_child = ctx.Pipe()
        reader = ctx.Process(target=_reader_process, args=(root, {"owner":"owner", "workspace_id":"w", "id":doc["id"]}, reader_child))
        reader.start()
        try:
            assert reader_parent.poll(10)
            fresh = reader_parent.recv()
            assert fresh["text"] == content
            assert fresh["receipt"][action]["outcome"] == "applied"
            assert fresh["historyCount"] >= index + 1
        finally:
            reader.join(5)
            if reader.is_alive(): reader.terminate(); reader.join(5)
        manifest_path = next(root.rglob("manifest.json"))
        manifest = json.loads(manifest_path.read_text())
        assert len([entry for entry in manifest["operations"] if entry.get("actionId") == action]) == 1
        doc["head"] = bridge._fingerprint(content)


@pytest.mark.asyncio
async def test_guarded_recovery_failure_blocks_ordinary_write_and_retry_recovers(tmp_path):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"blocked.md", "content":"before"})
    doc = created["doc"]
    journal = {"schemaVersion":1, "pending":{"pending": {"action_id":"pending", "actor_id":"a", "request_digest":"d", "target":{"owner":"owner", "workspace_id":"w", "id":doc["id"], "content":"intended", "before_head":doc["head"]}}}, "receipts":{}}
    bridge._guarded._write(journal)
    ordinary = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"ordinary.md", "content":"ordinary"})
    original_write_record = bridge._write_record
    bridge._write_record = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("temporary target recovery outage"))
    with pytest.raises(Exception, match="journal|recovery"):
        await bridge.call("write", {"owner":"owner", "workspace_id":"w", "id":ordinary["doc"]["id"], "base":ordinary["doc"]["head"], "content":"blocked"})
    bridge._write_record = original_write_record
    await bridge.call("get", {"owner":"owner", "workspace_id":"w", "id":doc["id"]})
    await bridge.call("write", {"owner":"owner", "workspace_id":"w", "id":ordinary["doc"]["id"], "base":ordinary["doc"]["head"], "content":"allowed"})
    assert json.loads(bridge._guarded.path.read_text())["receipts"]["pending"]["outcome"] == "applied"


def test_guarded_spawned_reset_and_progress_orders_are_guarded(tmp_path):
    def setup(root):
        bridge = LooseCopalBridge(root)
        source = asyncio.run(bridge.call("create", {"owner":"source", "workspace_id":"course", "name":"r.md", "content":"rev-1"}))
        target = asyncio.run(bridge.call("create", {"owner":"target", "workspace_id":"progress", "name":"p.md", "content":"before"}))
        source_doc, target_doc = source["doc"], target["doc"]
        reset = {"action_id":"reset", "actor_id":"teacher", "guards":[], "operations":[{"kind":"write", "owner":"source", "workspace_id":"course", "id":source_doc["id"], "head":source_doc["head"], "content":"rev-reset"}]}
        progress = {"action_id":"progress", "actor_id":"learner", "guards":[{"owner":"source", "workspace_id":"course", "id":source_doc["id"], "head":source_doc["head"]}], "operations":[{"kind":"write", "owner":"target", "workspace_id":"progress", "id":target_doc["id"], "head":target_doc["head"], "content":"done"}]}
        return bridge, source_doc, target_doc, reset, progress
    ctx = multiprocessing.get_context("spawn")
    root = tmp_path / "progress-first"
    bridge, source, target, reset, progress = setup(root)
    entered, release = ctx.Event(), ctx.Event()
    progress_parent, progress_child = ctx.Pipe(); reset_parent, reset_child = ctx.Pipe()
    progress_process = ctx.Process(target=_paused_guarded_writer, args=(root, progress, entered, release, progress_child))
    reset_process = ctx.Process(target=_ordinary_writer, args=(root, {"owner":"source", "workspace_id":"course", "id":source["id"], "base":source["head"], "content":"rev-reset"}, reset_child))
    progress_started = reset_started = False
    try:
        progress_process.start(); progress_started = True; assert entered.wait(10)
        reset_process.start(); reset_started = True; assert not reset_parent.poll(0.5), "ordinary reset must wait behind guarded progress"
        release.set()
        assert progress_parent.poll(10) and reset_parent.poll(10)
        assert progress_parent.recv()["outcome"] == "applied"
        assert reset_parent.recv()["outcome"] == "committed"
    finally:
        release.set()
        if progress_started: progress_process.join(5)
        if reset_started: reset_process.join(5)
        if progress_started and progress_process.is_alive(): progress_process.terminate(); progress_process.join(5)
        if reset_started and reset_process.is_alive(): reset_process.terminate(); reset_process.join(5)
    assert asyncio.run(bridge.call("get", {"owner":"target", "workspace_id":"progress", "id":target["id"]}))["text"] == "done"

    root = tmp_path / "reset-first"
    bridge, source, target, reset, progress = setup(root)
    reset_parent, reset_child = ctx.Pipe(); progress_parent, progress_child = ctx.Pipe()
    entered, release = ctx.Event(), ctx.Event()
    reset_process = ctx.Process(target=_paused_ordinary_writer, args=(root, {"owner":"source", "workspace_id":"course", "id":source["id"], "base":source["head"], "content":"rev-reset"}, entered, release, reset_child))
    progress_process = ctx.Process(target=_guarded_process, args=(root, progress, progress_child))
    reset_started = progress_started = False
    try:
        reset_process.start(); reset_started = True; assert entered.wait(10)
        progress_process.start(); progress_started = True; assert not progress_parent.poll(0.5), "guarded progress must wait behind ordinary reset"
        release.set()
        assert reset_parent.poll(10) and progress_parent.poll(10)
        assert reset_parent.recv()["outcome"] == "committed"
        assert progress_parent.recv()["outcome"] == "conflict"
    finally:
        release.set()
        if reset_started: reset_process.join(5)
        if progress_started: progress_process.join(5)
        if reset_started and reset_process.is_alive(): reset_process.terminate(); reset_process.join(5)
        if progress_started and progress_process.is_alive(): progress_process.terminate(); progress_process.join(5)
    assert asyncio.run(bridge.call("get", {"owner":"target", "workspace_id":"progress", "id":target["id"]}))["text"] == "before"


@pytest.mark.asyncio
async def test_guarded_decision_directory_sync_fault_has_no_target_effect(tmp_path, monkeypatch):
    bridge = LooseCopalBridge(tmp_path / "vaults")
    created = await bridge.call("create", {"owner":"owner", "workspace_id":"w", "name":"sync-dir.md", "content":"before"})
    doc = created["doc"]
    from src.openclank.copal_loose import LooseCopalRepository
    original = LooseCopalRepository._fsync_directory
    monkeypatch.setattr(LooseCopalRepository, "_fsync_directory", staticmethod(lambda path: (_ for _ in ()).throw(OSError("directory sync fault"))))
    with pytest.raises(Exception, match="sync|fault"):
        await bridge.call("commit_guarded", {"action_id":"dir-fault", "actor_id":"a", "guards":[], "operations":[{"kind":"write", "owner":"owner", "workspace_id":"w", "id":doc["id"], "head":doc["head"], "content":"after"}]})
    monkeypatch.setattr(LooseCopalRepository, "_fsync_directory", staticmethod(original))
    path = next((tmp_path / "vaults").rglob("sync-dir.md"))
    assert path.read_text() == "before"

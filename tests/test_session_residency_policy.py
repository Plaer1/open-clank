"""O13 residency sample: the REAL SessionManager and agent_runs on synthetic data, sampled before and after sessions go idle/complete.

Read-only: inventory_resident() must leave manager.sessions, every Session history and agent_runs._RUNS exactly as they were.
Numbers are printed as `SAMPLE ...` lines (run with -s) and recorded in the BQ-4 note."""
import asyncio
import importlib
import importlib.util
import json
import os
import tempfile
import uuid

_spec = importlib.util.spec_from_file_location(
    "isr", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "inspect_session_residency.py"))
isr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(isr)

OWNER = "u"


def _manager():
    """The real SessionManager on the process's database. Refuses to run unless that database is a temp file (the
    sandbox runner and CI set DATABASE_URL to one); it never reloads core.database, so the stats tables bind correctly."""
    import core.database as database
    url = str(database.engine.url)
    assert url.startswith("sqlite:///" + tempfile.gettempdir()) or "/sbxpy." in url, "refusing to run on a non-temporary database: " + url
    os.environ.setdefault("OPENCLANK_CONVERSATION_ARCHIVE_DB", database.engine.url.database + ".archive")
    from src.openclank.conversation_archive import configure_conversation_archive_owner_resolver, reset_conversation_archive_for_test
    reset_conversation_archive_for_test()
    configure_conversation_archive_owner_resolver(lambda v: "acct-" + v if v == OWNER else None,
                                                  lambda a: OWNER if a == "acct-" + OWNER else None)
    database.init_db()
    import core.session_manager as sm_mod
    manager = sm_mod.SessionManager()
    manager.sessions.clear()               # only the sessions this test creates are resident
    return manager, sm_mod


def _snapshot(manager, agent_runs):
    """Everything the sample must not change: identities, history lengths and run state."""
    return ({sid: (id(s), len(s.history), s.message_count, s.archived) for sid, s in manager.sessions.items()},
            {sid: (id(r), r.status, len(r.buffer), r.next_seq) for sid, r in agent_runs._RUNS.items()},
            len(agent_runs._DRAIN_TASKS))


def test_before_after_idle_sample_on_the_real_manager_is_read_only_and_closed():
    from core.models import ChatMessage
    from src import agent_runs
    manager, _ = _manager()
    tag = uuid.uuid4().hex[:6]
    S = {name: f"{name}-{tag}" for name in ("run", "done", "idle", "arch")}
    for sid in S.values():
        manager.create_session(session_id=sid, name=sid, endpoint_url="x", model="m", rag=False, owner=OWNER)
        for i in range(4):
            manager.add_message(sid, ChatMessage("user" if i % 2 == 0 else "assistant", f"{sid} message {i} " + "x" * 200))

    async def scenario():
        gate = asyncio.Event()

        async def waiting():
            yield "data: one\n\n"
            await gate.wait()
            yield "data: two\n\n"

        async def quick():
            yield "data: a\n\n"
            yield "data: [DONE]\n\n"

        agent_runs.start(S["run"], waiting())
        agent_runs.start(S["done"], quick())
        await asyncio.sleep(0.05)                           # s-done has finished; s-run is mid-stream

        before_state = _snapshot(manager, agent_runs)
        before = isr.inventory_resident(manager, runs=agent_runs, max_ids=2, full_ids=True)
        assert _snapshot(manager, agent_runs) == before_state, "the sample mutated state"
        assert before == isr.inventory_resident(manager, runs=agent_runs, max_ids=2, full_ids=True), "the sample is not repeatable"

        # lifecycle: the stream completes, one session is archived through the manager, one is deleted
        gate.set()
        await asyncio.sleep(0.05)
        manager.archive_session(S["arch"])
        manager.delete_session(S["idle"])
        after_state = _snapshot(manager, agent_runs)
        after = isr.inventory_resident(manager, runs=agent_runs, max_ids=2, full_ids=True)
        assert _snapshot(manager, agent_runs) == after_state
        return before, after

    try:
        before, after = asyncio.run(scenario())
    finally:
        asyncio.run(agent_runs.shutdown())
    print("SAMPLE before", json.dumps({k: before[k] for k in ("sessions", "agent_runs")}, sort_keys=True))
    print("SAMPLE after ", json.dumps({k: after[k] for k in ("sessions", "agent_runs")}, sort_keys=True))

    # shape: closed keys, bounded ids, no message text
    for report in (before, after):
        assert set(report) == {"version", "scope", "max_ids", "sessions", "agent_runs", "missing_observability"}
        assert set(report["sessions"]) == {"total", "flags", "metadata_only", "hydrated_active", "hydrated_idle"}
        assert all(len(report["sessions"][k]["top"]) <= 2 for k in ("metadata_only", "hydrated_active", "hydrated_idle"))
        assert len(report["agent_runs"]["top"]) <= 2
        assert "message 0" not in json.dumps(report) and report["missing_observability"] == isr.MISSING_OBSERVABILITY
    # before: 4 resident hydrated sessions; one has a running stream, one a finished stream retained, two never ran
    b = before["sessions"]
    assert (b["total"], b["hydrated_active"]["count"], b["hydrated_idle"]["count"], b["metadata_only"]["count"]) == (4, 1, 3, 0)
    assert before["agent_runs"]["by_status"] == {"running": 1, "done": 1} and before["agent_runs"]["terminal_runs_retained"] == 1
    # after: no running stream; both runs retained as terminal (buffers kept until the 180 s evict grace); the archived session
    # stays resident (the manager has no eviction); the deleted one is gone
    a = after["sessions"]
    assert (a["total"], a["hydrated_active"]["count"], a["hydrated_idle"]["count"]) == (3, 0, 3)
    assert a["flags"]["archived"] == 1
    assert after["agent_runs"]["by_status"] == {"done": 2} and after["agent_runs"]["terminal_runs_retained"] == 2
    assert after["agent_runs"]["evict_timers_pending"] == 2 and after["agent_runs"]["buffer_frames"] > 0


def test_a_fresh_manager_holds_metadata_only_and_missing_observability_names_engine_structures():
    from core.models import ChatMessage
    manager, sm_mod = _manager()
    sid = "restart-" + uuid.uuid4().hex[:6]
    manager.create_session(session_id=sid, name="n", endpoint_url="x", model="m", rag=False, owner=OWNER)
    manager.add_message(sid, ChatMessage("user", "hello"))
    restarted = sm_mod.SessionManager()                    # same database, as after a process restart
    report = isr.inventory_resident(restarted, runs=None)
    assert sid in restarted.sessions and not restarted.sessions[sid].history
    assert report["sessions"]["metadata_only"]["count"] == len(restarted.sessions) and report["sessions"]["hydrated_idle"]["count"] == 0
    assert report["agent_runs"] == "not supplied"
    text = "\n".join(isr.MISSING_OBSERVABILITY)
    for name in ("SessionRunState.runners", "forkContexts", "outerFibers", "pendingOutcomes", "cancelling", "run-state.ts:54", "actor/spawn.ts:226"):
        assert name in text

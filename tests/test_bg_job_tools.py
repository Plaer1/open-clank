"""Tests for bg_jobs.kill and the manage_bg_jobs agent tool.

Process-free: the store/dir are redirected to tmp, _pid_alive is forced True so
seeded "running" jobs stay running through refresh(), and _kill is stubbed so no
real signal is sent. Jobs are scoped to a chat (session_id), which is the main
invariant under test.
"""
import asyncio
import json
import time

import pytest

from src import bg_jobs
from src.agent_tools.bg_job_tools import ManageBgJobsTool

_OWNER = "alice"
_WORKSPACE = "/workspace"


@pytest.fixture
def store(tmp_path, monkeypatch):
    jobs_dir = tmp_path / "bg_jobs"
    jobs_dir.mkdir()
    monkeypatch.setattr(bg_jobs, "_STORE", tmp_path / "bg_jobs.json")
    monkeypatch.setattr(bg_jobs, "_JOBS_DIR", jobs_dir)
    monkeypatch.setattr(bg_jobs, "_pid_alive", lambda pid: True)
    killed: list = []
    monkeypatch.setattr(bg_jobs, "_kill", lambda pid: killed.append(pid))
    return {"dir": jobs_dir, "killed": killed}


def _seed(
    session_id="sess-a",
    status="running",
    job_id="job0001",
    output="",
    pid=4321,
    owner=_OWNER,
    workspace=_WORKSPACE,
):
    rec = {
        "id": job_id, "session_id": session_id, "command": "sleep 60",
        "owner": owner, "workspace": workspace,
        "status": status, "pid": pid, "started_at": time.time(),
        "ended_at": None if status == "running" else time.time(),
        "exit_code": None if status == "running" else 0,
        "max_runtime_s": 3600, "followed_up": False,
        "log_path": str(bg_jobs._JOBS_DIR / f"{job_id}.log"),
        "exit_path": str(bg_jobs._JOBS_DIR / f"{job_id}.exit"),
    }
    if output:
        (bg_jobs._JOBS_DIR / f"{job_id}.log").write_text(output, encoding="utf-8")
    jobs = bg_jobs._load()
    jobs[job_id] = rec
    bg_jobs._save(jobs)
    return rec


def _run(args, session_id="sess-a", owner=_OWNER, workspace=_WORKSPACE):
    return asyncio.run(ManageBgJobsTool().execute(
        json.dumps(args),
        {"session_id": session_id, "owner": owner, "workspace": workspace},
    ))


# ── bg_jobs.kill ────────────────────────────────────────────────────────────

def test_launch_requires_complete_scope(store):
    with pytest.raises(ValueError, match="authenticated owner"):
        bg_jobs.launch(
            "true",
            session_id="sess-a",
            owner=None,
            workspace=_WORKSPACE,
        )


def test_kill_marks_killed_and_suppresses_followup(store):
    _seed(job_id="job0001", pid=4321)
    rec = bg_jobs.kill("job0001")
    assert rec["status"] == "failed"
    assert rec["killed"] is True
    assert rec["exit_code"] == -1
    # followed_up True so the monitor won't ALSO auto-continue a deliberate kill.
    assert rec["followed_up"] is True
    assert store["killed"] == [4321]


def test_kill_unknown_job_returns_none(store):
    assert bg_jobs.kill("nope") is None


def test_kill_finished_job_is_noop(store):
    _seed(job_id="done01", status="done")
    rec = bg_jobs.kill("done01")
    assert rec["status"] == "done"
    assert store["killed"] == []  # no signal sent to an already-finished job


def test_result_text_reports_killed(store):
    rec = _seed(job_id="job0001")
    bg_jobs.kill("job0001")
    assert "killed" in bg_jobs.result_text(bg_jobs.get("job0001")).lower()


def test_followup_claim_is_atomic_and_token_fenced(store):
    _seed(job_id="done01", status="done")
    first = bg_jobs.claim_pending_followups("monitor-a")
    second = bg_jobs.claim_pending_followups("monitor-b")

    assert [row["id"] for row in first] == ["done01"]
    assert second == []
    token = first[0]["_claim_token"]
    assert not bg_jobs.finish_followup("done01", "stale-token")
    assert bg_jobs.finish_followup("done01", token)
    assert bg_jobs.claim_pending_followups("monitor-b") == []


def test_followup_retry_budget_is_bounded(store, monkeypatch):
    _seed(job_id="done01", status="done")
    now = 1000.0
    monkeypatch.setattr(bg_jobs.time, "time", lambda: now)

    for attempt in range(bg_jobs._FOLLOWUP_MAX_ATTEMPTS):
        claimed = bg_jobs.claim_pending_followups("monitor")
        assert len(claimed) == 1
        state = bg_jobs.fail_followup("done01", claimed[0]["_claim_token"], "down")
        if attempt + 1 < bg_jobs._FOLLOWUP_MAX_ATTEMPTS:
            assert state == "pending"
            now = float(bg_jobs._load()["done01"]["next_followup_at"])
        else:
            assert state == "failed"

    record = bg_jobs._load()["done01"]
    assert record["followed_up"] is True
    assert bg_jobs.claim_pending_followups("monitor") == []


# ── manage_bg_jobs tool ─────────────────────────────────────────────────────

def test_no_session_is_rejected(store):
    out = asyncio.run(ManageBgJobsTool().execute('{"action":"list"}', {"session_id": None}))
    assert "error" in out


def test_owner_and_workspace_are_required(store):
    assert "error" in _run({"action": "list"}, owner="")
    assert "error" in _run({"action": "list"}, workspace="")


def test_list_empty(store):
    assert "No background jobs" in _run({"action": "list"})["output"]


def test_list_scoped_to_session(store):
    _seed(session_id="sess-a", job_id="aaaa")
    _seed(session_id="sess-b", job_id="bbbb")
    out = _run({"action": "list"}, session_id="sess-a")["output"]
    assert "aaaa" in out and "bbbb" not in out


def test_list_is_fail_closed_across_owner_and_workspace(store, tmp_path):
    workspace_a = str(tmp_path / "a")
    workspace_b = str(tmp_path / "b")
    _seed(job_id="alice-a", owner="alice", workspace=workspace_a)
    _seed(job_id="bob-a", owner="bob", workspace=workspace_a)
    _seed(job_id="alice-b", owner="alice", workspace=workspace_b)
    _seed(job_id="legacy", owner="", workspace="")

    out = _run(
        {"action": "list"},
        owner="alice",
        workspace=workspace_a,
    )["output"]
    assert "alice-a" in out
    assert "bob-a" not in out
    assert "alice-b" not in out
    assert "legacy" not in out


def test_output_returns_captured_log(store):
    _seed(job_id="job0001", output="hello from the job\n")
    out = _run({"action": "output", "job_id": "job0001"})["output"]
    assert "hello from the job" in out


def test_output_cross_session_denied(store):
    _seed(session_id="sess-a", job_id="job0001", output="secret")
    out = _run({"action": "output", "job_id": "job0001"}, session_id="sess-b")
    assert "error" in out and "secret" not in out.get("error", "")


def test_output_requires_exact_owner_and_workspace(store, tmp_path):
    workspace_a = str(tmp_path / "a")
    _seed(
        session_id="sess-a",
        job_id="job0001",
        output="secret",
        owner="alice",
        workspace=workspace_a,
    )

    assert "error" in _run(
        {"action": "output", "job_id": "job0001"},
        owner="bob",
        workspace=workspace_a,
    )
    assert "error" in _run(
        {"action": "output", "job_id": "job0001"},
        owner="alice",
        workspace=str(tmp_path / "b"),
    )
    assert "secret" in _run(
        {"action": "output", "job_id": "job0001"},
        owner="alice",
        workspace=workspace_a,
    )["output"]


def test_wait_does_not_expose_internal_paths(store):
    _seed(job_id="done01", status="done")
    session = _run(
        {"action": "wait", "job_id": "done01", "timeout_s": 0},
    )["session"]
    assert session["state"] == "done"
    assert session["elapsed_s"] >= 0
    assert session["owner"] == _OWNER
    assert session["workspace"] == _WORKSPACE
    assert "log_path" not in session
    assert "exit_path" not in session
    assert "pid" not in session


def test_delete_removes_record_and_retained_output(store):
    _seed(job_id="done01", status="done", output="retained")
    log_path = bg_jobs._JOBS_DIR / "done01.log"
    out = _run({"action": "delete", "job_id": "done01"})
    assert "Deleted" in out["output"]
    assert bg_jobs.get("done01") is None
    assert not log_path.exists()


def test_session_delete_removes_only_exact_owner_jobs(store):
    _seed(
        session_id="sess-a",
        job_id="alice-a",
        owner="alice",
        output="owned",
        pid=101,
    )
    _seed(
        session_id="sess-a",
        job_id="bob-a",
        owner="bob",
        output="foreign owner",
        pid=202,
    )
    _seed(
        session_id="sess-b",
        job_id="alice-b",
        owner="alice",
        output="foreign session",
        pid=303,
    )

    assert bg_jobs.delete_for_session_owner(
        session_id="sess-a",
        owner="alice",
    ) == 1
    assert "alice-a" not in bg_jobs._load()
    assert not (bg_jobs._JOBS_DIR / "alice-a.log").exists()
    assert set(bg_jobs._load()) == {"bob-a", "alice-b"}
    assert store["killed"] == [101]


def test_kill_via_tool(store):
    _seed(job_id="job0001", pid=999)
    out = _run({"action": "kill", "job_id": "job0001"})
    assert "Killed" in out["output"]
    assert store["killed"] == [999]
    assert bg_jobs.get("job0001")["killed"] is True


def test_kill_cross_session_denied(store):
    _seed(session_id="sess-a", job_id="job0001")
    out = _run({"action": "kill", "job_id": "job0001"}, session_id="sess-b")
    assert "error" in out
    assert store["killed"] == []  # never touched another chat's job


def test_operation_rechecks_scope_after_lookup(store, monkeypatch):
    _seed(job_id="job0001", owner="bob", workspace=_WORKSPACE)
    monkeypatch.setattr(
        bg_jobs,
        "get_scoped",
        lambda *args, **kwargs: {
            **bg_jobs._load()["job0001"],
            "owner": _OWNER,
        },
    )

    out = _run({"action": "kill", "job_id": "job0001"})

    assert "error" in out
    assert store["killed"] == []
    assert bg_jobs._load()["job0001"]["status"] == "running"


def test_low_level_job_operations_enforce_scope(store):
    _seed(job_id="job0001", owner="bob", workspace=_WORKSPACE, output="secret")
    scope = {
        "session_id": "sess-a",
        "owner": _OWNER,
        "workspace": _WORKSPACE,
    }

    with pytest.raises(KeyError):
        bg_jobs.tail("job0001", **scope)
    assert bg_jobs.write("job0001", "nope", **scope) is False
    assert bg_jobs.kill("job0001", **scope) is None
    assert bg_jobs.delete("job0001", **scope) is False
    assert bg_jobs._load()["job0001"]["status"] == "running"


def test_kill_requires_job_id(store):
    assert "error" in _run({"action": "kill"})


def test_unknown_action(store):
    assert "error" in _run({"action": "frobnicate"})


def test_action_aliases(store):
    _seed(job_id="job0001", output="aliased")
    # 'read' aliases to output, 'jobs' to list, 'stop' to kill
    assert "aliased" in _run({"action": "read", "job_id": "job0001"})["output"]
    assert "job0001" in _run({"action": "jobs"})["output"]
    assert "Killed" in _run({"action": "stop", "job_id": "job0001"})["output"]


# ── intent classifier: short bg-job commands must not be dropped as low-signal ─
# A short imperative ("kill that job") otherwise trips the low-signal gate, which
# skips tool retrieval entirely and never surfaces manage_bg_jobs (the live bug
# this feature hit). These lock in that bg-job control reaches the files domain.


@pytest.mark.parametrize("msg", [
    "stop the job",
    "kill that job",
    "Now kill that background job.",
    "is the job done?",
    "check the job output",
    "list my jobs",
    "kill the bg task",
])
def test_bg_job_commands_are_not_low_signal(msg):
    from src.agent_loop import _classify_agent_request, _DOMAIN_TOOL_MAP
    r = _classify_agent_request([{"role": "user", "content": msg}], msg)
    assert r["low_signal"] is False
    assert "files" in r["domains"]
    # files domain seeds manage_bg_jobs, so it gets offered to the model.
    assert "manage_bg_jobs" in _DOMAIN_TOOL_MAP["files"]


@pytest.mark.parametrize("msg", [
    "run this in the background",   # launching, not managing
    "find me a job listing",        # unrelated use of "job"
])
def test_non_bg_messages_do_not_trip_files_domain(msg):
    from src.agent_loop import _classify_agent_request
    r = _classify_agent_request([{"role": "user", "content": msg}], msg)
    assert "files" not in r["domains"]

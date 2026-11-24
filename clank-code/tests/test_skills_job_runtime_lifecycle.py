import asyncio
import json

import pytest


@pytest.mark.asyncio
async def test_nuke_quiesce_keeps_confirmed_skill_job_preview_stable(monkeypatch, tmp_path):
    from routes import skills_routes

    state_path = tmp_path / "skill-audit-jobs.json"
    monkeypatch.setattr(skills_routes, "_SKILL_JOB_STATE_PATH", state_path)
    monkeypatch.setattr(
        skills_routes,
        "_skill_test_jobs",
        {("alice", "skill"): {"status": "running", "cancel": False}},
    )
    monkeypatch.setattr(skills_routes, "_skill_audit_jobs", {})
    monkeypatch.setattr(skills_routes, "_skill_test_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_audit_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_job_persistence_suspended", set())
    skills_routes._persist_skill_job_state()
    before = state_path.read_bytes()

    async def worker():
        await asyncio.sleep(60)

    task = asyncio.create_task(worker())
    skills_routes.track_skill_test_handle(("alice", "skill"), task)
    await skills_routes.quiesce_owner_skill_job_handles("alice", persist=False)
    await asyncio.sleep(0)

    before_rows = json.loads(before)
    after_rows = json.loads(state_path.read_bytes())
    assert before_rows["test"][0] == after_rows["test"][0]
    assert skills_routes._skill_test_jobs[("alice", "skill")]["status"] == "cancelled"

    # A completion from another owner must not serialize Alice's in-memory
    # cancellation into the still-confirmed durable preview.
    skills_routes._skill_test_jobs[("bob", "other")] = {"status": "done"}
    skills_routes._persist_skill_job_state()
    after_rows = json.loads(state_path.read_bytes())
    assert before_rows["test"][0] == after_rows["test"][0]


@pytest.mark.asyncio
async def test_cancelled_scheduled_audit_is_not_reported_done(monkeypatch, tmp_path):
    from routes import skills_routes

    monkeypatch.setattr(
        skills_routes,
        "_SKILL_JOB_STATE_PATH",
        tmp_path / "skill-audit-jobs.json",
    )
    jobs = {
        ("alice",): {
            "status": "running",
            "cancel": False,
            "log": [],
            "results": [],
            "done": 0,
            "current": None,
        }
    }
    monkeypatch.setattr(skills_routes, "_skill_audit_jobs", jobs)
    monkeypatch.setattr(skills_routes, "_skill_audit_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_test_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_job_persistence_suspended", set())

    class Manager:
        def load(self, *, owner):
            return [{"name": "skill"}]

    async def blocked_skill(*_args, **_kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(skills_routes, "_audit_one_skill", blocked_skill)
    monkeypatch.setattr(
        skills_routes,
        "_resolve_audit_models",
        lambda owner: (type("Route", (), {"provider_model_id": "worker"})(), None),
    )
    task = asyncio.create_task(
        skills_routes._run_scheduled_owner_skill_audit(
            Manager(), owner="alice", names=["skill"]
        )
    )
    for _ in range(20):
        if ("alice",) in skills_routes._skill_audit_handles:
            break
        await asyncio.sleep(0)
    await skills_routes.quiesce_owner_skill_job_handles("alice", persist=False)
    result = await task
    assert result["status"] == "cancelled"


def test_skill_runtime_sync_reloads_renamed_owner_without_dropping_bob(
    monkeypatch, tmp_path
):
    from routes import skills_routes

    state_path = tmp_path / "skill-audit-jobs.json"
    state_path.write_text(
        json.dumps(
            {
                "test": [
                    {"key": ["alice-new", "skill"], "job": {"status": "done"}},
                    {"key": ["bob", "other"], "job": {"status": "done"}},
                ],
                "audit": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(skills_routes, "_SKILL_JOB_STATE_PATH", state_path)
    monkeypatch.setattr(
        skills_routes,
        "_skill_test_jobs",
        {
            ("alice", "skill"): {"status": "done"},
            ("bob", "other"): {"status": "done"},
        },
    )
    monkeypatch.setattr(skills_routes, "_skill_audit_jobs", {})
    monkeypatch.setattr(skills_routes, "_skill_test_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_audit_handles", {})
    monkeypatch.setattr(skills_routes, "_skill_job_persistence_suspended", set())

    counts = skills_routes.synchronize_owner_skill_job_runtime("alice", "alice-new")

    assert counts == {"test": 1, "audit": 0}
    assert ("alice", "skill") not in skills_routes._skill_test_jobs
    assert skills_routes._skill_test_jobs[("alice-new", "skill")]["status"] == "done"
    assert skills_routes._skill_test_jobs[("bob", "other")]["status"] == "done"

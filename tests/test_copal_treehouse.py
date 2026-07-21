from datetime import UTC, datetime, timedelta

import pytest

from src.openclank.copal_treehouse import (
    TreeHouseError,
    apply_legacy_migration,
    apply_treehouse_command,
    compute_treehouse_projections,
    new_treehouse_state,
    plan_legacy_migration,
    public_treehouse_snapshot,
)


def run(state, command_type, payload=None, *, actor="owner", command_id=None, now=None, revision=None):
    return apply_treehouse_command(
        state,
        {"type": command_type, "payload": payload or {}},
        actor_id=actor,
        command_id=command_id or f"cmd:{command_type}:{state['revision']}",
        expected_revision=state["revision"] if revision is None else revision,
        now=now,
    )


def setup_course():
    state = new_treehouse_state("owner@example.test", now=datetime(2026, 7, 10, tzinfo=UTC))
    state, _, _ = run(state, "profile.create", {"id": "learner", "displayName": "Learner", "roles": ["learner"]})
    state, result, _ = run(state, "skill.create", {"id": "skill:foundation", "title": "Foundation", "masteryThreshold": 60})
    state, result, _ = run(state, "skill.create", {"id": "skill:advanced", "title": "Advanced", "prerequisiteIds": ["skill:foundation"]})
    state, result, _ = run(state, "course.create", {"id": "course:one", "title": "Course One", "description": "A real course"})
    state, result, _ = run(state, "module.create", {"id": "module:one", "courseId": "course:one", "title": "Module One"})
    state, _, _ = run(state, "activity.create", {"id": "activity:foundation", "moduleId": "module:one", "title": "Foundation lesson", "content": "# Learn", "points": 60, "skillIds": ["skill:foundation"]})
    state, _, _ = run(state, "activity.create", {"id": "activity:advanced", "moduleId": "module:one", "title": "Advanced lesson", "points": 20, "skillIds": ["skill:advanced"]})
    state, _, _ = run(state, "assignment.create", {"id": "assignment:one", "moduleId": "module:one", "title": "Show the work", "prompt": "Explain it", "maxPoints": 100, "skillIds": ["skill:advanced"]})
    state, _, _ = run(state, "assignment.publish", {"assignmentId": "assignment:one"})
    state, _, _ = run(state, "course.publish", {"courseId": "course:one"})
    return state


def test_complete_author_learner_grade_and_gamification_workflow_is_event_derived():
    state = setup_course()
    state, _, _ = run(state, "badge.create", {"id": "badge:century", "title": "Century", "criteria": {"type": "points", "threshold": 100}})
    state, _, _ = run(state, "quest.create", {"id": "quest:path", "title": "Path", "activityIds": ["activity:foundation", "activity:advanced"], "assignmentIds": ["assignment:one"], "rewardPoints": 25})
    state, _, _ = run(state, "enrollment.enroll", {"courseId": "course:one"}, actor="learner")

    with pytest.raises(TreeHouseError) as blocked:
        run(state, "activity.complete", {"activityId": "activity:advanced"}, actor="learner")
    assert blocked.value.code == "prerequisites_unmet"

    day = datetime(2026, 7, 10, 12, tzinfo=UTC)
    state, foundation, _ = run(state, "activity.complete", {"activityId": "activity:foundation"}, actor="learner", now=day)
    state, advanced, _ = run(state, "activity.complete", {"activityId": "activity:advanced"}, actor="learner", now=day + timedelta(days=1))
    before_duplicate = len(state["events"])
    state, duplicate, _ = run(state, "activity.complete", {"activityId": "activity:advanced"}, actor="learner", command_id="cmd:duplicate")
    assert duplicate["alreadyComplete"] is True
    assert len(state["events"]) == before_duplicate

    state, submitted, _ = run(state, "submission.submit", {"assignmentId": "assignment:one", "answer": {"text": "because"}}, actor="learner", now=day + timedelta(days=2))
    state, graded, _ = run(state, "submission.grade", {"submissionId": submitted["submissionId"], "score": 80, "feedback": "Good"}, now=day + timedelta(days=2))
    projection = compute_treehouse_projections(state)["learners"]["learner"]

    assert projection["points"] == 60 + 20 + 80 + 25
    assert projection["skills"]["skill:foundation"]["points"] == 60
    assert projection["skills"]["skill:advanced"]["points"] == 100
    assert projection["skills"]["skill:advanced"]["unlocked"] is True
    assert projection["courses"]["course:one"]["completed"] == 3
    assert projection["courses"]["course:one"]["total"] == 3
    assert projection["courses"]["course:one"]["percent"] == 100
    assert projection["courses"]["course:one"]["complete"] is True
    assert "modules" in projection["courses"]["course:one"]
    assert projection["badges"][0]["badgeId"] == "badge:century"
    assert projection["quests"][0]["questId"] == "quest:path"
    assert projection["streak"] == 3
    assert all(item.get("eventId") for item in projection["pointEvidence"])
    assert foundation["eventId"] != advanced["eventId"] != graded["eventId"]


def test_command_replay_stale_revision_roles_and_cycles_are_enforced():
    state = new_treehouse_state("owner")
    state, _, _ = run(state, "profile.create", {"id": "learner", "displayName": "Learner", "roles": ["learner"]})
    state, first, changed = run(state, "course.create", {"title": "One"}, command_id="fixed")
    replay_state, replay, replay_changed = run(state, "course.create", {"title": "Different"}, command_id="fixed", revision=0)
    assert replay_state is state
    assert replay["replayed"] is True
    assert replay["courseId"] == first["courseId"]
    assert replay_changed is False

    with pytest.raises(TreeHouseError) as stale:
        run(state, "course.create", {"title": "Stale"}, command_id="stale", revision=0)
    assert stale.value.code == "stale"
    with pytest.raises(TreeHouseError) as forbidden:
        run(state, "course.create", {"title": "No"}, actor="learner")
    assert forbidden.value.status == 403

    state, _, _ = run(state, "skill.create", {"id": "skill:a", "title": "A"})
    state, _, _ = run(state, "skill.create", {"id": "skill:b", "title": "B", "prerequisiteIds": ["skill:a"]})
    with pytest.raises(TreeHouseError) as cycle:
        run(state, "skill.update", {"skillId": "skill:a", "prerequisiteIds": ["skill:b"]})
    assert cycle.value.code == "prerequisite_cycle"


def test_publish_gates_submission_ownership_and_regrade_supersedes_points():
    state = new_treehouse_state("owner")
    state, _, _ = run(state, "profile.create", {"id": "learner", "displayName": "L", "roles": ["learner"]})
    state, course, _ = run(state, "course.create", {"id": "course:x", "title": "X"})
    with pytest.raises(TreeHouseError) as incomplete:
        run(state, "course.publish", {"courseId": "course:x"})
    assert incomplete.value.code == "publish_incomplete"

    state = setup_course()
    state, _, _ = run(state, "enrollment.enroll", {"courseId": "course:one"}, actor="learner")
    state, _, _ = run(state, "activity.complete", {"activityId": "activity:foundation"}, actor="learner")
    state, _, _ = run(state, "activity.complete", {"activityId": "activity:advanced"}, actor="learner")
    state, submitted, _ = run(state, "submission.submit", {"assignmentId": "assignment:one", "answer": "answer"}, actor="learner")
    with pytest.raises(TreeHouseError):
        run(state, "submission.grade", {"submissionId": submitted["submissionId"], "score": 10}, actor="learner")
    state, _, _ = run(state, "submission.grade", {"submissionId": submitted["submissionId"], "score": 90})
    state, _, _ = run(state, "submission.grade", {"submissionId": submitted["submissionId"], "score": 30})
    projection = compute_treehouse_projections(state)["learners"]["learner"]
    assert projection["points"] == 60 + 20 + 30


def test_ordering_deadlines_and_enrollment_completion_state_are_enforced():
    state = setup_course()
    state, _, _ = run(state, "module.create", {"id": "module:two", "courseId": "course:one", "title": "Module Two"})
    state, _, _ = run(state, "activity.create", {"id": "activity:third", "moduleId": "module:two", "title": "Third"})
    state, _, _ = run(state, "course.reorder_modules", {"courseId": "course:one", "moduleIds": ["module:two", "module:one"]})
    assert state["courses"]["course:one"]["moduleIds"] == ["module:two", "module:one"]
    with pytest.raises(TreeHouseError) as bad_order:
        run(state, "course.reorder_modules", {"courseId": "course:one", "moduleIds": ["module:one"]})
    assert bad_order.value.code == "invalid_order"

    state, _, _ = run(state, "enrollment.enroll", {"courseId": "course:one"}, actor="learner")
    state, _, _ = run(state, "activity.complete", {"activityId": "activity:foundation"}, actor="learner")
    state, _, _ = run(state, "activity.complete", {"activityId": "activity:advanced"}, actor="learner")
    state, _, _ = run(state, "activity.complete", {"activityId": "activity:third"}, actor="learner")
    state, submitted, _ = run(state, "submission.submit", {"assignmentId": "assignment:one", "answer": "ok"}, actor="learner")
    state, _, _ = run(state, "submission.grade", {"submissionId": submitted["submissionId"], "score": 80})
    assert state["enrollments"]["course:one:learner"]["status"] == "completed"
    assert any(event["type"] == "course.completed" for event in state["events"])

    expired = setup_course()
    expired, _, _ = run(expired, "assignment.update", {"assignmentId": "assignment:one", "dueAt": "2026-07-01T00:00:00Z"})
    expired, _, _ = run(expired, "enrollment.enroll", {"courseId": "course:one"}, actor="learner")
    expired, _, _ = run(expired, "activity.complete", {"activityId": "activity:foundation"}, actor="learner")
    with pytest.raises(TreeHouseError) as deadline:
        run(expired, "submission.submit", {"assignmentId": "assignment:one", "answer": "late"}, actor="learner", now=datetime(2026, 7, 10, tzinfo=UTC))
    assert deadline.value.code == "assignment_past_due"


def test_evidence_review_and_role_filtered_snapshot():
    state = new_treehouse_state("owner")
    state, _, _ = run(state, "profile.create", {"id": "learner", "displayName": "L", "roles": ["learner"]})
    state, _, _ = run(state, "profile.create", {"id": "other", "displayName": "O", "roles": ["learner"]})
    state, _, _ = run(state, "skill.create", {"id": "skill:a", "title": "A", "evidencePoints": 35})
    state, _, _ = run(state, "badge.create", {"id": "badge:evidence", "title": "Evidence", "criteria": {"type": "points", "threshold": 30}})
    state, submitted, _ = run(state, "evidence.submit", {"skillId": "skill:a", "description": "Artifact"}, actor="learner")
    state, reviewed, _ = run(state, "evidence.review", {"evidenceId": submitted["evidenceId"], "decision": "approved", "note": "Verified"})
    approved = compute_treehouse_projections(state)["learners"]["learner"]
    assert approved["points"] == 35
    assert approved["badges"] == [{"badgeId": "badge:evidence", "evidenceEventIds": [reviewed["eventId"]]}]
    learner_view = public_treehouse_snapshot(state, "learner")
    assert set(learner_view["state"]["evidence"]) == {submitted["evidenceId"]}
    assert learner_view["permissions"]["grade"] is False
    assert learner_view["projection"]["leaderboard"] == []
    assert reviewed["status"] == "approved"

    state, rejected, _ = run(state, "evidence.review", {"evidenceId": submitted["evidenceId"], "decision": "rejected", "note": "Correction"})
    corrected = compute_treehouse_projections(state)["learners"]["learner"]
    assert rejected["status"] == "rejected"
    assert corrected["points"] == 0
    assert corrected["skills"]["skill:a"]["points"] == 0
    assert corrected["badges"] == []
    assert corrected["pointEvidence"] == []


def test_legacy_migration_is_dry_run_idempotent_and_preserves_sources():
    state = new_treehouse_state("owner")
    docs = [{
        "id": "DOC", "head": "head-1", "kind": "lesson", "name": "Lesson.md", "text": "# Lesson\n- [ ] Prove it",
        "frontmatter": {"course": "Course", "skill": "Skill"},
        "treehouse": {"course": "Course", "skill": "Skill", "prerequisite": None},
        "tasks": [{"id": "DOC:2", "text": "Prove it", "done": False}],
    }]
    plan = plan_legacy_migration(docs, state)
    assert plan["counts"] == {"documents": 1, "courses": 1, "skills": 1, "tasks": 1}
    migrated, result, changed = apply_legacy_migration(state, plan, actor_id="owner", command_id="migration", expected_revision=0)
    assert changed is True
    assert result["imported"]["activities"] == 1
    assert next(iter(migrated["activities"].values()))["sourceDocumentId"] == "DOC"
    assert plan_legacy_migration(docs, migrated)["candidates"] == []
    replayed, replay_result, replay_changed = apply_legacy_migration(migrated, plan, actor_id="owner", command_id="migration", expected_revision=0)
    assert replayed is migrated and replay_changed is False and replay_result["replayed"] is True

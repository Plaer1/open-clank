"""Additive learning semantics; owner curriculum stays the single authority.

No legacy writes or second quest/reward engine. Contract version is separate
from the aggregate/SQLite schema and portable course-package version.
"""
from __future__ import annotations

from typing import Any, Literal, TypedDict

CourseType = Literal["tutorial", "quest"]
CompletionBasis = Literal["content-traversal", "required-work"]
LEARNING_CONTRACT_VERSION = 1
COMPLETION_RULE_VERSION = 1
XP_RULE_VERSION = "legacy-event-points-v1"


class CompletionCriteria(TypedDict):
    activityIds: list[str]
    assignmentIds: list[str]
    assignmentMode: Literal["graded", "submitted"]


class CourseLearningContract(TypedDict):
    learningContractVersion: int
    courseType: CourseType
    classification: str
    completionBasis: CompletionBasis
    completionCriteria: CompletionCriteria
    curriculumRevision: int
    completionRuleVersion: int
    xpRuleVersion: str
    missionDefinitionIds: list[str]


def published_items(state: dict[str, Any], course_id: str, collection: str) -> list[str]:
    return [str(item["id"]) for item in state.get(collection, {}).values()
            if item.get("courseId") == course_id and item.get("status") == "published"
            and not item.get("deletedAt")]


def activity_has_work(activity: dict[str, Any]) -> bool:
    # Existing Field Guide explicitly records practice and verified/self-check
    # outcomes. Do not call those click-through lessons merely from their type.
    return bool(activity.get("requiredWork") or activity.get("completion") == "verified"
                or (activity.get("completion") == "self-check" and activity.get("practice")))


def course_learning_contract(state: dict[str, Any], course_id: str) -> CourseLearningContract:
    course = state["courses"][course_id]
    activities = published_items(state, course_id, "activities")
    assignments = published_items(state, course_id, "assignments")
    explicit = course.get("courseType") in {"tutorial", "quest"}
    work = bool(assignments or course.get("requiredMissionIds") or course.get("requiredEvidenceIds")
                or course.get("requiredSkillIds") or any(activity_has_work(state["activities"][key])
                or any(state.get("skills", {}).get(skill_id, {}).get("prerequisiteIds")
                       for skill_id in state["activities"][key].get("skillIds", [])) for key in activities))
    # A course-bound mission predicate is work, but cross-course reward
    # predicates do not silently gate a formerly independent course.
    mission_ids = []
    for key, item in state.get("quests", {}).items():
        if item.get("status") != "active" or item.get("deletedAt"):
            continue
        refs = [("activities", ident) for ident in item.get("activityIds", [])] + [("assignments", ident) for ident in item.get("assignmentIds", [])]
        if item.get("courseId") == course_id or (refs and all(state.get(kind, {}).get(ident, {}).get("courseId") == course_id for kind, ident in refs)):
            mission_ids.append(key)
    work = work or bool(mission_ids)
    course_type: CourseType = course["courseType"] if explicit else ("quest" if work else "tutorial")
    criteria = course.get("completionCriteria")
    if isinstance(criteria, dict):
        required_activities = [key for key in criteria.get("activityIds", []) if key in activities]
        required_assignments = [key for key in criteria.get("assignmentIds", []) if key in assignments]
        mode = criteria.get("assignmentMode", "graded")
    else:
        required_activities, required_assignments, mode = activities, assignments, "graded"
    if course_type == "tutorial" and explicit:
        required_assignments = []
    return {"learningContractVersion": LEARNING_CONTRACT_VERSION,
            "courseType": course_type, "classification": "explicit" if explicit else "legacy-inferred",
            "completionBasis": "content-traversal" if course_type == "tutorial" else "required-work",
            "completionCriteria": {"activityIds": required_activities, "assignmentIds": required_assignments,
                                   "assignmentMode": mode},
            "curriculumRevision": int(course.get("curriculumRevision", state.get("revision", 0))),
            "missionDefinitionIds": mission_ids,
            "completionRuleVersion": COMPLETION_RULE_VERSION, "xpRuleVersion": XP_RULE_VERSION}


def validate_course_contract(state: dict[str, Any], course_id: str, *, publishing: bool = False) -> None:
    course = state["courses"][course_id]
    if "courseType" in course and course["courseType"] not in {"tutorial", "quest"}:
        raise ValueError("Course type must be tutorial or quest")
    if "learningContractVersion" in course and course["learningContractVersion"] != LEARNING_CONTRACT_VERSION:
        raise ValueError("Unsupported learning contract version")
    criteria = course.get("completionCriteria")
    if criteria is not None:
        if not isinstance(criteria, dict) or set(criteria) - {"activityIds", "assignmentIds", "assignmentMode"}:
            raise ValueError("Invalid completion criteria")
        if criteria.get("assignmentMode", "graded") not in {"graded", "submitted"}:
            raise ValueError("Assignment completion must be graded or submitted")
        for field, collection in (("activityIds", "activities"), ("assignmentIds", "assignments")):
            refs = criteria.get(field, [])
            if not isinstance(refs, list) or len(refs) > 2_000 or any(not isinstance(key, str) for key in refs) or len(set(refs)) != len(refs):
                raise ValueError("Completion criteria must contain unique bounded ID lists")
            if any(key not in state[collection] or state[collection][key].get("courseId") != course_id
                   or state[collection][key].get("deletedAt") for key in refs):
                raise ValueError("Completion criteria references missing or foreign course work")
    if not publishing or "courseType" not in course:
        return
    if criteria:
        for field, collection in (("activityIds", "activities"), ("assignmentIds", "assignments")):
            if any(state[collection][key].get("status") != "published" and not state[collection][key].get("deletedAt")
                   for key in criteria.get(field, [])):
                raise ValueError("Publish required course work before publishing its completion criteria")
    contract = course_learning_contract(state, course_id)
    required = contract["completionCriteria"]
    if course["courseType"] == "tutorial":
        if (criteria or {}).get("assignmentIds") or any(activity_has_work(state["activities"][key]) for key in required["activityIds"]):
            raise ValueError("Tutorial completion cannot require work; choose Quest or exclude optional work")
        if not required["activityIds"]:
            raise ValueError("Tutorial needs published content to traverse")
    elif not required["assignmentIds"] and not any(activity_has_work(state["activities"][key]) for key in required["activityIds"]):
        raise ValueError("Quest needs published required work, not only content traversal")


def course_progress(state: dict[str, Any], course_id: str, profile_id: str,
                    completed_events: dict[tuple[str, str], dict[str, Any]],
                    latest_grades: dict[str, dict[str, Any]],
                    submitted_events: dict[tuple[str, str], dict[str, Any]],
                    opened_events: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    contract = course_learning_contract(state, course_id)
    criteria = contract["completionCriteria"]
    activity_ids, assignment_ids = criteria["activityIds"], criteria["assignmentIds"]
    activity_evidence = {key: completed_events[(profile_id, key)] for key in activity_ids if (profile_id, key) in completed_events}
    assignment_evidence = {}
    for event in latest_grades.values():
        if event.get("subjectId") == profile_id and event.get("data", {}).get("assignmentId") in assignment_ids:
            assignment = state["assignments"].get(event["data"]["assignmentId"], {})
            if event["data"].get("percent", 0) >= assignment.get("passPercent", 0):
                assignment_evidence[event["data"]["assignmentId"]] = event
    if criteria["assignmentMode"] == "submitted":
        for key in assignment_ids:
            if (profile_id, key) in submitted_events:
                assignment_evidence[key] = submitted_events[(profile_id, key)]
    done, total = len(activity_evidence) + len(assignment_evidence), len(activity_ids) + len(assignment_ids)
    complete = total > 0 and done == total
    opened = opened_events.get((profile_id, course_id))
    relevant = [event for (subject, key), event in completed_events.items()
                if subject == profile_id and state["activities"].get(key, {}).get("courseId") == course_id]
    latest = max(relevant + ([opened] if opened else []), key=lambda event: event["at"], default=None)
    resume = latest.get("data", {}).get("activityId") if latest and latest.get("type") == "course.opened" else (latest.get("entityId") if latest else None)
    generation = max(int(state.get("progressResets", {}).get(f"{profile_id}:{course_id}", 0)),
                     int(state.get("progressResets", {}).get(f"{profile_id}:*", 0)))
    evidence = [*activity_evidence.values(), *assignment_evidence.values()]
    # A completed assessment-bearing course has reviewed evidence. This is
    # not a mastery claim; percent/grades and completion basis remain visible.
    verified = complete and bool(assignment_ids) and all(event["type"] == "submission.graded" for event in assignment_evidence.values())
    course = state["courses"][course_id]
    unmet = [] if course.get("freeExploration") else [key for key in course.get("prerequisites", [])
            if state.get("enrollments", {}).get(f"{key}:{profile_id}", {}).get("status") != "completed"]
    availability = course.get("status", "draft") if course.get("status") != "published" else ("locked" if unmet else "available")
    return {**contract, "availability": {"status": availability, "unmetPrerequisiteCourseIds": unmet},
            "completed": done, "total": total,
            "percent": round(done / total * 100) if total else 0, "complete": complete,
            "state": "completed" if complete else ("in-progress" if done else ("opened" if opened else "available")),
            "verified": verified, "verificationBasis": "reviewed-assignment" if verified else None,
            "requiredActivityIds": activity_ids, "requiredAssignmentIds": assignment_ids,
            "missingActivityIds": [key for key in activity_ids if key not in activity_evidence],
            "missingAssignmentIds": [key for key in assignment_ids if key not in assignment_evidence],
            "evidenceEventIds": [event["id"] for event in evidence], "resetGeneration": generation,
            "resumeActivityId": resume}

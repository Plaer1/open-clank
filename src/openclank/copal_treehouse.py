"""First-party TreeHouse LMMS domain over one optimistic Redb aggregate.

The domain combines course workflows and evidence-based progression without
depending on either reference application at runtime.  Immutable learning
events are canonical for progress, points, levels, badges, quests, streaks,
and analytics; projections are rebuilt for every read/command.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable


SCHEMA_VERSION = 1
MAX_EVENTS = 50_000
MAX_PROCESSED_COMMANDS = 2_000
EVENT_NAMESPACE = uuid.UUID("036a68db-a103-5bc4-962b-97f13b70b447")
LEGACY_NAMESPACE = uuid.UUID("a60667fd-0c92-5b5f-ac62-bde89d19497b")
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ROLES = {"admin", "instructor", "learner"}


class TreeHouseError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid", status: int = 400, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), **self.details}


def _now(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")


def _id(value: Any, field: str = "id") -> str:
    text = str(value or "").strip()
    if not ID_RE.fullmatch(text):
        raise TreeHouseError(f"Invalid {field}", code="invalid_id", details={"field": field})
    return text


def _new_id(prefix: str) -> str:
    return f"{prefix}:{uuid.uuid4()}"


def _text(value: Any, field: str, *, maximum: int = 8_192, required: bool = True) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise TreeHouseError(f"{field} is required", code="required", details={"field": field})
    if len(text) > maximum:
        raise TreeHouseError(f"{field} is too long", code="too_long", details={"field": field, "maximum": maximum})
    return text


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise TreeHouseError(f"{field} must be an integer", code="invalid_number", details={"field": field}) from exc
    if parsed < minimum or parsed > maximum:
        raise TreeHouseError(f"{field} must be between {minimum} and {maximum}", code="out_of_range", details={"field": field})
    return parsed


def _iso_datetime(value: Any, field: str, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        if required: raise TreeHouseError(f"{field} is required", code="required", details={"field": field})
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TreeHouseError(f"{field} must be an ISO date/time", code="invalid_datetime", details={"field": field}) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _list_ids(value: Any, field: str, *, maximum: int = 128) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise TreeHouseError(f"{field} must be a list of at most {maximum} IDs", code="invalid_list", details={"field": field})
    return list(dict.fromkeys(_id(item, field) for item in value))


def new_treehouse_state(owner: str, *, now: datetime | None = None) -> dict[str, Any]:
    timestamp = _now(now)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": 0,
        "createdAt": timestamp,
        "updatedAt": timestamp,
        "profiles": {
            "owner": {
                "id": "owner",
                "displayName": owner or "Owner",
                "roles": ["admin", "instructor", "learner"],
                "active": True,
                "createdAt": timestamp,
            }
        },
        "courses": {},
        "modules": {},
        "activities": {},
        "enrollments": {},
        "assignments": {},
        "submissions": {},
        "skills": {},
        "evidence": {},
        "badges": {},
        "quests": {},
        "events": [],
        "processedCommands": {},
        "migrations": {"legacyFrontmatter": {"sources": {}}},
        "extensions": {},
    }


def validate_treehouse_state(state: dict[str, Any]) -> None:
    if not isinstance(state, dict) or state.get("schemaVersion") != SCHEMA_VERSION:
        raise TreeHouseError("Unsupported TreeHouse state schema", code="unsupported_schema", status=409)
    for key in ("profiles", "courses", "modules", "activities", "enrollments", "assignments", "submissions", "skills", "evidence", "badges", "quests", "processedCommands"):
        if not isinstance(state.get(key), dict):
            raise TreeHouseError(f"TreeHouse state field {key} is corrupt", code="corrupt_state", status=409)
    if not isinstance(state.get("events"), list) or len(state["events"]) > MAX_EVENTS:
        raise TreeHouseError("TreeHouse event ledger is corrupt or over its safety limit", code="corrupt_state", status=409)


def _profile(state: dict[str, Any], actor_id: str) -> dict[str, Any]:
    actor = state["profiles"].get(actor_id)
    if not actor or not actor.get("active", True):
        raise TreeHouseError("TreeHouse profile not found or inactive", code="profile_not_found", status=404)
    return actor


def _has_role(profile: dict[str, Any], *roles: str) -> bool:
    return bool(set(profile.get("roles") or []) & set(roles))


def _require_role(state: dict[str, Any], actor_id: str, *roles: str) -> dict[str, Any]:
    actor = _profile(state, actor_id)
    if not _has_role(actor, *roles):
        raise TreeHouseError("This TreeHouse profile lacks permission", code="forbidden", status=403, details={"requiredRoles": list(roles)})
    return actor


def _course_author(state: dict[str, Any], actor_id: str, course_id: str) -> dict[str, Any]:
    actor = _require_role(state, actor_id, "admin", "instructor")
    course = state["courses"].get(course_id)
    if not course:
        raise TreeHouseError("Course not found", code="course_not_found", status=404)
    if "admin" not in actor["roles"] and actor_id not in course.get("authorIds", []):
        raise TreeHouseError("Only a course author can change this course", code="forbidden", status=403)
    return course


def _entity(state: dict[str, Any], collection: str, entity_id: str, label: str) -> dict[str, Any]:
    value = state[collection].get(entity_id)
    if not value:
        raise TreeHouseError(f"{label} not found", code=f"{label.lower()}_not_found", status=404)
    return value


def _emit(
    state: dict[str, Any],
    *,
    command_id: str,
    ordinal: int,
    event_type: str,
    actor_id: str,
    subject_id: str,
    entity_type: str,
    entity_id: str,
    data: dict[str, Any],
    at: str,
) -> dict[str, Any]:
    if len(state["events"]) >= MAX_EVENTS:
        raise TreeHouseError("TreeHouse event safety limit reached; export/archive before continuing", code="event_limit", status=409)
    event = {
        "id": str(uuid.uuid5(EVENT_NAMESPACE, f"{command_id}\n{ordinal}\n{event_type}\n{entity_id}")),
        "commandId": command_id,
        "type": event_type,
        "actorId": actor_id,
        "subjectId": subject_id,
        "entityType": entity_type,
        "entityId": entity_id,
        "at": at,
        "data": copy.deepcopy(data),
    }
    state["events"].append(event)
    return event


def _published_course(state: dict[str, Any], course_id: str) -> dict[str, Any]:
    course = _entity(state, "courses", course_id, "Course")
    if course.get("status") != "published":
        raise TreeHouseError("Course is not published", code="course_not_published", status=409)
    return course


def _enrollment_key(course_id: str, profile_id: str) -> str:
    return f"{course_id}:{profile_id}"


def _submission_key(assignment_id: str, profile_id: str) -> str:
    return f"{assignment_id}:{profile_id}"


def _require_enrollment(state: dict[str, Any], course_id: str, actor_id: str) -> dict[str, Any]:
    enrollment = state["enrollments"].get(_enrollment_key(course_id, actor_id))
    if not enrollment or enrollment.get("status") not in {"active", "completed"}:
        raise TreeHouseError("Enroll in the course before doing learner work", code="not_enrolled", status=403)
    return enrollment


def _skill_cycle(skills: dict[str, dict[str, Any]]) -> list[str] | None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(skill_id: str, path: list[str]) -> list[str] | None:
        if skill_id in visiting:
            return [*path[path.index(skill_id):], skill_id]
        if skill_id in visited:
            return None
        visiting.add(skill_id)
        for dependency in skills[skill_id].get("prerequisiteIds", []):
            if dependency in skills:
                cycle = walk(dependency, [*path, skill_id])
                if cycle:
                    return cycle
        visiting.remove(skill_id)
        visited.add(skill_id)
        return None

    for skill_id in skills:
        cycle = walk(skill_id, [])
        if cycle:
            return cycle
    return None


def _event_day(event: dict[str, Any]) -> date:
    return date.fromisoformat(str(event["at"])[:10])


def _proficiency(points: int, thresholds: list[int] | None = None) -> tuple[str, int]:
    levels = thresholds or [0, 25, 60, 100]
    labels = ["novice", "apprentice", "adept", "master"]
    index = max(index for index, threshold in enumerate(levels) if points >= threshold)
    return labels[min(index, len(labels) - 1)], index


def compute_treehouse_projections(state: dict[str, Any]) -> dict[str, Any]:
    """Rebuild all learner projections from the immutable event ledger."""
    validate_treehouse_state(state)
    by_profile: dict[str, dict[str, Any]] = {}
    latest_grades: dict[str, dict[str, Any]] = {}
    completed_events: dict[tuple[str, str], dict[str, Any]] = {}
    latest_evidence_reviews: dict[str, dict[str, Any]] = {}
    learning_days: dict[str, set[date]] = {}

    def learner(profile_id: str) -> dict[str, Any]:
        return by_profile.setdefault(profile_id, {
            "profileId": profile_id,
            "points": 0,
            "pointEvidence": [],
            "completedActivityIds": [],
            "gradedSubmissionIds": [],
            "skills": {},
            "badges": [],
            "quests": [],
            "streak": 0,
            "courses": {},
        })

    for event in state["events"]:
        subject = event.get("subjectId")
        if subject not in state["profiles"]:
            continue
        if event["type"] == "activity.completed":
            completed_events[(subject, event["entityId"])] = event
            learning_days.setdefault(subject, set()).add(_event_day(event))
        elif event["type"] == "submission.graded":
            latest_grades[event["entityId"]] = event
            learning_days.setdefault(subject, set()).add(_event_day(event))
        elif event["type"] in {"evidence.approved", "evidence.rejected"}:
            latest_evidence_reviews[event["entityId"]] = event
        elif event["type"] == "submission.submitted":
            learning_days.setdefault(subject, set()).add(_event_day(event))

    approved_evidence = {
        evidence_id: event
        for evidence_id, event in latest_evidence_reviews.items()
        if event["type"] == "evidence.approved"
    }
    for event in approved_evidence.values():
        learning_days.setdefault(event["subjectId"], set()).add(_event_day(event))

    point_events: dict[str, list[tuple[dict[str, Any], int, str, list[dict[str, Any]]]]] = {}
    for (profile_id, activity_id), event in completed_events.items():
        activity = state["activities"].get(activity_id, {})
        points = int(event.get("data", {}).get("points", activity.get("points", 10)))
        skills = event.get("data", {}).get("skills", [])
        point_events.setdefault(profile_id, []).append((event, points, f"Completed {activity.get('title', activity_id)}", skills))
        learner(profile_id)["completedActivityIds"].append(activity_id)
    for submission_id, event in latest_grades.items():
        profile_id = event["subjectId"]
        points = int(event.get("data", {}).get("points", 0))
        point_events.setdefault(profile_id, []).append((event, points, "Graded assignment", event.get("data", {}).get("skills", [])))
        learner(profile_id)["gradedSubmissionIds"].append(submission_id)
    for evidence_id, event in approved_evidence.items():
        profile_id = event["subjectId"]
        point_events.setdefault(profile_id, []).append((event, int(event["data"].get("points", 0)), "Approved skill evidence", event["data"].get("skills", [])))

    for profile_id in state["profiles"]:
        projection = learner(profile_id)
        skill_points: dict[str, int] = {}
        skill_evidence: dict[str, list[str]] = {}
        for event, points, explanation, skills in point_events.get(profile_id, []):
            projection["points"] += points
            projection["pointEvidence"].append({"eventId": event["id"], "points": points, "explanation": explanation})
            for entry in skills:
                skill_id = entry.get("skillId")
                if skill_id not in state["skills"]:
                    continue
                awarded = int(entry.get("points", points))
                skill_points[skill_id] = skill_points.get(skill_id, 0) + awarded
                skill_evidence.setdefault(skill_id, []).append(event["id"])

        for skill_id, skill in state["skills"].items():
            points = skill_points.get(skill_id, 0)
            level, level_index = _proficiency(points, skill.get("thresholds"))
            prerequisites = skill.get("prerequisiteIds", [])
            unlocked = all(skill_points.get(dep, 0) >= int(state["skills"].get(dep, {}).get("masteryThreshold", 60)) for dep in prerequisites)
            projection["skills"][skill_id] = {
                "points": points,
                "level": level,
                "levelIndex": level_index,
                "unlocked": unlocked,
                "prerequisiteIds": prerequisites,
                "evidenceEventIds": skill_evidence.get(skill_id, []),
            }

        completed = set(projection["completedActivityIds"])
        graded_assignments = {
            state["submissions"].get(submission_id, {}).get("assignmentId")
            for submission_id in projection["gradedSubmissionIds"]
        }
        for quest_id, quest in state["quests"].items():
            if quest.get("status") != "active":
                continue
            activities_ok = set(quest.get("activityIds", [])).issubset(completed)
            assignments_ok = set(quest.get("assignmentIds", [])).issubset(graded_assignments)
            if activities_ok and assignments_ok:
                evidence_ids = [completed_events[(profile_id, item)]["id"] for item in quest.get("activityIds", []) if (profile_id, item) in completed_events]
                evidence_ids += [latest_grades[key]["id"] for key in projection["gradedSubmissionIds"] if state["submissions"].get(key, {}).get("assignmentId") in quest.get("assignmentIds", [])]
                reward = int(quest.get("rewardPoints", 0))
                projection["quests"].append({"questId": quest_id, "rewardPoints": reward, "evidenceEventIds": evidence_ids})
                projection["points"] += reward
                projection["pointEvidence"].append({"eventId": f"quest:{quest_id}", "points": reward, "explanation": f"Completed quest {quest.get('title', quest_id)}", "evidenceEventIds": evidence_ids})

        for badge_id, badge in state["badges"].items():
            criteria = badge.get("criteria", {})
            achieved = False
            evidence_ids: list[str] = []
            if criteria.get("type") == "points":
                achieved = projection["points"] >= int(criteria.get("threshold", 0))
                evidence_ids = [item["eventId"] for item in projection["pointEvidence"]]
            elif criteria.get("type") == "skill":
                progress = projection["skills"].get(criteria.get("skillId"), {})
                achieved = progress.get("points", 0) >= int(criteria.get("threshold", 60))
                evidence_ids = progress.get("evidenceEventIds", [])
            elif criteria.get("type") == "course":
                course_id = criteria.get("courseId")
                required = {item["id"] for item in state["activities"].values() if item.get("courseId") == course_id and item.get("status") == "published"}
                achieved = bool(required) and required.issubset(completed)
                evidence_ids = [completed_events[(profile_id, item)]["id"] for item in required if (profile_id, item) in completed_events]
            elif criteria.get("type") == "quest":
                match = next((item for item in projection["quests"] if item["questId"] == criteria.get("questId")), None)
                achieved = match is not None
                evidence_ids = match.get("evidenceEventIds", []) if match else []
            if achieved:
                projection["badges"].append({"badgeId": badge_id, "evidenceEventIds": evidence_ids})

        days = sorted(learning_days.get(profile_id, set()), reverse=True)
        if days:
            streak = 1
            for previous, current in zip(days, days[1:]):
                if previous - current == timedelta(days=1): streak += 1
                else: break
            projection["streak"] = streak

        for course_id, course in state["courses"].items():
            published_activities = [item["id"] for item in state["activities"].values() if item.get("courseId") == course_id and item.get("status") == "published"]
            published_assignments = [item["id"] for item in state["assignments"].values() if item.get("courseId") == course_id and item.get("status") == "published"]
            done = len(set(published_activities) & completed) + len(set(published_assignments) & graded_assignments)
            total = len(published_activities) + len(published_assignments)
            projection["courses"][course_id] = {"completed": done, "total": total, "percent": round(done / total * 100) if total else 0, "complete": total > 0 and done == total}

    leaderboard = sorted(
        ({"profileId": profile_id, "displayName": state["profiles"][profile_id]["displayName"], "points": data["points"]} for profile_id, data in by_profile.items()),
        key=lambda item: (-item["points"], item["displayName"].casefold(), item["profileId"]),
    )
    course_analytics = {}
    for course_id in state["courses"]:
        enrollments = [item for item in state["enrollments"].values() if item.get("courseId") == course_id]
        percents = [by_profile.get(item["profileId"], {}).get("courses", {}).get(course_id, {}).get("percent", 0) for item in enrollments]
        grades = [event["data"].get("percent", 0) for event in latest_grades.values() if state["assignments"].get(event["data"].get("assignmentId"), {}).get("courseId") == course_id]
        course_analytics[course_id] = {
            "enrollments": len(enrollments),
            "averageProgress": round(sum(percents) / len(percents), 1) if percents else 0,
            "averageGrade": round(sum(grades) / len(grades), 1) if grades else None,
            "completedLearners": sum(1 for value in percents if value == 100),
        }
    return {"learners": by_profile, "leaderboard": leaderboard, "courses": course_analytics, "eventCount": len(state["events"])}


def _require_skills_unlocked(state: dict[str, Any], actor_id: str, skill_ids: list[str]) -> None:
    projection = compute_treehouse_projections(state)["learners"].get(actor_id, {"skills": {}})
    blocked = [skill_id for skill_id in skill_ids if not projection["skills"].get(skill_id, {}).get("unlocked", True)]
    if blocked:
        raise TreeHouseError("Complete prerequisite skills first", code="prerequisites_unmet", status=409, details={"skillIds": blocked})


def _command_result(state: dict[str, Any], command: dict[str, Any], actor_id: str, command_id: str, at: str) -> dict[str, Any]:
    kind = _text(command.get("type"), "type", maximum=80)
    payload = command.get("payload") or {}
    if not isinstance(payload, dict):
        raise TreeHouseError("payload must be an object", code="invalid_payload")
    ordinal = 0

    def emit(event_type: str, subject_id: str, entity_type: str, entity_id: str, data: dict[str, Any]) -> dict[str, Any]:
        nonlocal ordinal
        ordinal += 1
        return _emit(state, command_id=command_id, ordinal=ordinal, event_type=event_type, actor_id=actor_id, subject_id=subject_id, entity_type=entity_type, entity_id=entity_id, data=data, at=at)

    def sync_course_completion(course_id: str, profile_id: str) -> None:
        enrollment = state["enrollments"].get(_enrollment_key(course_id, profile_id))
        if not enrollment: return
        complete = compute_treehouse_projections(state)["learners"].get(profile_id, {}).get("courses", {}).get(course_id, {}).get("complete", False)
        if complete and enrollment.get("status") != "completed":
            enrollment["status"] = "completed"; enrollment["completedAt"] = at; enrollment["updatedAt"] = at
            emit("course.completed", profile_id, "course", course_id, {"enrollmentId": enrollment["id"]})
        elif not complete and enrollment.get("status") == "completed":
            enrollment["status"] = "active"; enrollment.pop("completedAt", None); enrollment["updatedAt"] = at

    def sync_course_enrollments(course_id: str) -> None:
        for enrollment in list(state["enrollments"].values()):
            if enrollment.get("courseId") == course_id:
                sync_course_completion(course_id, enrollment["profileId"])

    if kind == "profile.create":
        _require_role(state, actor_id, "admin")
        profile_id = _id(payload.get("id") or _new_id("profile"), "profileId")
        if profile_id in state["profiles"]:
            raise TreeHouseError("Profile already exists", code="already_exists", status=409)
        roles = sorted(set(payload.get("roles") or ["learner"]))
        if not roles or not set(roles).issubset(ROLES):
            raise TreeHouseError("Invalid profile roles", code="invalid_roles")
        state["profiles"][profile_id] = {"id": profile_id, "displayName": _text(payload.get("displayName"), "displayName", maximum=128), "roles": roles, "active": True, "createdAt": at}
        emit("profile.created", profile_id, "profile", profile_id, {"roles": roles})
        return {"profileId": profile_id}

    if kind == "profile.update":
        _require_role(state, actor_id, "admin")
        profile_id = _id(payload.get("profileId"), "profileId")
        profile = _profile(state, profile_id)
        if "displayName" in payload: profile["displayName"] = _text(payload["displayName"], "displayName", maximum=128)
        if "roles" in payload:
            roles = sorted(set(payload["roles"]))
            if not roles or not set(roles).issubset(ROLES): raise TreeHouseError("Invalid profile roles", code="invalid_roles")
            profile["roles"] = roles
        if "active" in payload and profile_id != "owner": profile["active"] = bool(payload["active"])
        emit("profile.updated", profile_id, "profile", profile_id, {"roles": profile["roles"]})
        return {"profileId": profile_id}

    if kind == "course.create":
        _require_role(state, actor_id, "admin", "instructor")
        course_id = _id(payload.get("id") or _new_id("course"), "courseId")
        if course_id in state["courses"]: raise TreeHouseError("Course already exists", code="already_exists", status=409)
        state["courses"][course_id] = {
            "id": course_id, "title": _text(payload.get("title"), "title", maximum=256),
            "description": _text(payload.get("description"), "description", maximum=16_384, required=False),
            "tags": [_text(item, "tag", maximum=64) for item in (payload.get("tags") or [])[:32]],
            "status": "draft", "authorIds": [actor_id], "moduleIds": [], "createdAt": at, "updatedAt": at,
        }
        emit("course.created", actor_id, "course", course_id, {})
        return {"courseId": course_id}

    if kind in {"course.update", "course.publish", "course.archive", "course.author.add", "course.reorder_modules"}:
        course_id = _id(payload.get("courseId"), "courseId")
        course = _course_author(state, actor_id, course_id)
        if kind == "course.update":
            if "title" in payload: course["title"] = _text(payload["title"], "title", maximum=256)
            if "description" in payload: course["description"] = _text(payload["description"], "description", maximum=16_384, required=False)
            event_type = "course.updated"
        elif kind == "course.publish":
            modules = [state["modules"].get(item) for item in course.get("moduleIds", [])]
            if not modules or any(
                not module or not (
                    any(state["activities"].get(item, {}).get("status") == "published" for item in module.get("activityIds", []))
                    or any(state["assignments"].get(item, {}).get("status") == "published" for item in module.get("assignmentIds", []))
                ) for module in modules
            ):
                raise TreeHouseError("A published course needs at least one populated module", code="publish_incomplete", status=409)
            course["status"] = "published"; event_type = "course.published"
        elif kind == "course.archive":
            course["status"] = "archived"; event_type = "course.archived"
        elif kind == "course.author.add":
            profile_id = _id(payload.get("profileId"), "profileId"); profile = _profile(state, profile_id)
            if not _has_role(profile, "admin", "instructor"): raise TreeHouseError("Course authors need the instructor role", code="invalid_roles")
            course["authorIds"] = list(dict.fromkeys([*course.get("authorIds", []), profile_id])); event_type = "course.author_added"
        else:
            ordered = _list_ids(payload.get("moduleIds"), "moduleIds")
            if set(ordered) != set(course.get("moduleIds", [])): raise TreeHouseError("Module order must contain every course module exactly once", code="invalid_order")
            course["moduleIds"] = ordered
            for index, module_id in enumerate(ordered): state["modules"][module_id]["order"] = index
            event_type = "course.modules_reordered"
        course["updatedAt"] = at
        emit(event_type, actor_id, "course", course_id, {})
        sync_course_enrollments(course_id)
        return {"courseId": course_id, "status": course["status"]}

    if kind == "module.create":
        course_id = _id(payload.get("courseId"), "courseId")
        course = _course_author(state, actor_id, course_id)
        module_id = _id(payload.get("id") or _new_id("module"), "moduleId")
        if module_id in state["modules"]: raise TreeHouseError("Module already exists", code="already_exists", status=409)
        state["modules"][module_id] = {"id": module_id, "courseId": course_id, "title": _text(payload.get("title"), "title", maximum=256), "description": _text(payload.get("description"), "description", maximum=8_192, required=False), "activityIds": [], "assignmentIds": [], "order": len(course["moduleIds"]), "createdAt": at, "updatedAt": at}
        course["moduleIds"].append(module_id); course["updatedAt"] = at
        emit("module.created", actor_id, "module", module_id, {"courseId": course_id})
        return {"moduleId": module_id}

    if kind == "module.update":
        module_id = _id(payload.get("moduleId"), "moduleId")
        module = _entity(state, "modules", module_id, "Module")
        _course_author(state, actor_id, module["courseId"])
        if "title" in payload: module["title"] = _text(payload["title"], "title", maximum=256)
        if "description" in payload: module["description"] = _text(payload["description"], "description", maximum=8_192, required=False)
        module["updatedAt"] = at
        emit("module.updated", actor_id, "module", module_id, {})
        return {"moduleId": module_id}

    if kind == "module.reorder_items":
        module_id = _id(payload.get("moduleId"), "moduleId"); module = _entity(state, "modules", module_id, "Module")
        _course_author(state, actor_id, module["courseId"])
        activity_ids = _list_ids(payload.get("activityIds"), "activityIds")
        assignment_ids = _list_ids(payload.get("assignmentIds"), "assignmentIds")
        if set(activity_ids) != set(module.get("activityIds", [])) or set(assignment_ids) != set(module.get("assignmentIds", [])):
            raise TreeHouseError("Item order must contain every module item exactly once", code="invalid_order")
        module["activityIds"] = activity_ids; module["assignmentIds"] = assignment_ids; module["updatedAt"] = at
        for index, item in enumerate(activity_ids): state["activities"][item]["order"] = index
        emit("module.items_reordered", actor_id, "module", module_id, {})
        return {"moduleId": module_id}

    if kind in {"activity.create", "activity.update"}:
        if kind == "activity.create":
            module_id = _id(payload.get("moduleId"), "moduleId")
            module = _entity(state, "modules", module_id, "Module")
            _course_author(state, actor_id, module["courseId"])
            activity_id = _id(payload.get("id") or _new_id("activity"), "activityId")
            if activity_id in state["activities"]: raise TreeHouseError("Activity already exists", code="already_exists", status=409)
            skill_ids = _list_ids(payload.get("skillIds"), "skillIds")
            missing = [item for item in skill_ids if item not in state["skills"]]
            if missing: raise TreeHouseError("Activity references missing skills", code="skill_not_found", details={"skillIds": missing})
            activity_type = str(payload.get("activityType") or "lesson")
            if activity_type not in {"lesson", "markdown", "video", "resource", "custom"}: raise TreeHouseError("Unsupported activity type", code="invalid_activity_type")
            state["activities"][activity_id] = {
                "id": activity_id, "courseId": module["courseId"], "moduleId": module_id,
                "title": _text(payload.get("title"), "title", maximum=256),
                "content": _text(payload.get("content"), "content", maximum=262_144, required=False),
                "activityType": activity_type, "status": str(payload.get("status") or "published"),
                "points": _integer(payload.get("points", 10), "points", maximum=10_000), "skillIds": skill_ids,
                "order": len(module["activityIds"]), "createdAt": at, "updatedAt": at,
            }
            if state["activities"][activity_id]["status"] not in {"draft", "published", "archived"}: raise TreeHouseError("Invalid activity status", code="invalid_status")
            module["activityIds"].append(activity_id); module["updatedAt"] = at
            event_type = "activity.created"
        else:
            activity_id = _id(payload.get("activityId"), "activityId")
            activity = _entity(state, "activities", activity_id, "Activity")
            _course_author(state, actor_id, activity["courseId"])
            for field, maximum in (("title", 256), ("content", 262_144)):
                if field in payload: activity[field] = _text(payload[field], field, maximum=maximum, required=field == "title")
            if "points" in payload: activity["points"] = _integer(payload["points"], "points", maximum=10_000)
            if "skillIds" in payload:
                skill_ids = _list_ids(payload["skillIds"], "skillIds")
                if any(item not in state["skills"] for item in skill_ids): raise TreeHouseError("Activity references missing skills", code="skill_not_found")
                activity["skillIds"] = skill_ids
            if "status" in payload:
                if payload["status"] not in {"draft", "published", "archived"}: raise TreeHouseError("Invalid activity status", code="invalid_status")
                activity["status"] = payload["status"]
            activity["updatedAt"] = at; event_type = "activity.updated"
        emit(event_type, actor_id, "activity", activity_id, {})
        sync_course_enrollments(state["activities"][activity_id]["courseId"])
        return {"activityId": activity_id}

    if kind == "enrollment.enroll":
        _require_role(state, actor_id, "learner")
        course_id = _id(payload.get("courseId"), "courseId"); _published_course(state, course_id)
        key = _enrollment_key(course_id, actor_id)
        if key in state["enrollments"]:
            return {"enrollmentId": key, "alreadyEnrolled": True}
        state["enrollments"][key] = {"id": key, "courseId": course_id, "profileId": actor_id, "status": "active", "enrolledAt": at, "updatedAt": at}
        emit("enrollment.created", actor_id, "enrollment", key, {"courseId": course_id})
        return {"enrollmentId": key}

    if kind == "activity.complete":
        _require_role(state, actor_id, "learner")
        activity_id = _id(payload.get("activityId"), "activityId")
        activity = _entity(state, "activities", activity_id, "Activity")
        _published_course(state, activity["courseId"]); _require_enrollment(state, activity["courseId"], actor_id)
        if activity.get("status") != "published": raise TreeHouseError("Activity is not published", code="activity_not_published", status=409)
        if any(event["type"] == "activity.completed" and event["subjectId"] == actor_id and event["entityId"] == activity_id for event in state["events"]):
            return {"activityId": activity_id, "alreadyComplete": True}
        _require_skills_unlocked(state, actor_id, activity.get("skillIds", []))
        skills = [{"skillId": item, "points": activity["points"]} for item in activity.get("skillIds", [])]
        event = emit("activity.completed", actor_id, "activity", activity_id, {"courseId": activity["courseId"], "points": activity["points"], "skills": skills})
        sync_course_completion(activity["courseId"], actor_id)
        return {"activityId": activity_id, "eventId": event["id"]}

    if kind == "assignment.create":
        module_id = _id(payload.get("moduleId"), "moduleId")
        module = _entity(state, "modules", module_id, "Module"); _course_author(state, actor_id, module["courseId"])
        assignment_id = _id(payload.get("id") or _new_id("assignment"), "assignmentId")
        if assignment_id in state["assignments"]: raise TreeHouseError("Assignment already exists", code="already_exists", status=409)
        skill_ids = _list_ids(payload.get("skillIds"), "skillIds")
        if any(item not in state["skills"] for item in skill_ids): raise TreeHouseError("Assignment references missing skills", code="skill_not_found")
        state["assignments"][assignment_id] = {
            "id": assignment_id, "courseId": module["courseId"], "moduleId": module_id,
            "title": _text(payload.get("title"), "title", maximum=256),
            "prompt": _text(payload.get("prompt"), "prompt", maximum=65_536, required=False),
            "status": "draft", "dueAt": _iso_datetime(payload.get("dueAt"), "dueAt"),
            "maxPoints": _integer(payload.get("maxPoints", 100), "maxPoints", minimum=1, maximum=10_000),
            "skillIds": skill_ids, "allowRetries": bool(payload.get("allowRetries", True)),
            "maxAttempts": _integer(payload.get("maxAttempts", 0), "maxAttempts", maximum=100),
            "createdAt": at, "updatedAt": at,
        }
        module["assignmentIds"].append(assignment_id); module["updatedAt"] = at
        emit("assignment.created", actor_id, "assignment", assignment_id, {})
        return {"assignmentId": assignment_id}

    if kind in {"assignment.update", "assignment.publish"}:
        assignment_id = _id(payload.get("assignmentId"), "assignmentId")
        assignment = _entity(state, "assignments", assignment_id, "Assignment"); _course_author(state, actor_id, assignment["courseId"])
        if kind == "assignment.update":
            for field, maximum in (("title", 256), ("prompt", 65_536), ("dueAt", 128)):
                if field in payload: assignment[field] = _iso_datetime(payload[field], "dueAt") if field == "dueAt" else _text(payload[field], field, maximum=maximum, required=field == "title")
            if "maxPoints" in payload:
                maximum = _integer(payload["maxPoints"], "maxPoints", minimum=1, maximum=10_000)
                existing_max = max((item.get("grade") or 0 for item in state["submissions"].values() if item.get("assignmentId") == assignment_id), default=0)
                if maximum < existing_max: raise TreeHouseError("Maximum points cannot be below an existing grade", code="grade_out_of_range", status=409)
                assignment["maxPoints"] = maximum
            event_type = "assignment.updated"
        else:
            assignment["status"] = "published"; event_type = "assignment.published"
        assignment["updatedAt"] = at; emit(event_type, actor_id, "assignment", assignment_id, {})
        sync_course_enrollments(assignment["courseId"])
        return {"assignmentId": assignment_id, "status": assignment["status"]}

    if kind == "submission.submit":
        _require_role(state, actor_id, "learner")
        assignment_id = _id(payload.get("assignmentId"), "assignmentId")
        assignment = _entity(state, "assignments", assignment_id, "Assignment")
        _published_course(state, assignment["courseId"]); _require_enrollment(state, assignment["courseId"], actor_id)
        if assignment.get("status") != "published": raise TreeHouseError("Assignment is not published", code="assignment_not_published", status=409)
        if assignment.get("dueAt") and datetime.fromisoformat(assignment["dueAt"].replace("Z", "+00:00")) < datetime.fromisoformat(at.replace("Z", "+00:00")):
            raise TreeHouseError("Assignment deadline has passed", code="assignment_past_due", status=409)
        _require_skills_unlocked(state, actor_id, assignment.get("skillIds", []))
        key = _submission_key(assignment_id, actor_id); submission = state["submissions"].get(key)
        if submission and submission.get("status") == "graded" and not assignment.get("allowRetries"):
            raise TreeHouseError("This assignment does not allow another attempt", code="retry_not_allowed", status=409)
        attempts = int(submission.get("attempts", 0)) + 1 if submission else 1
        if assignment.get("maxAttempts") and attempts > assignment["maxAttempts"]:
            raise TreeHouseError("Maximum assignment attempts reached", code="attempt_limit", status=409)
        answer = payload.get("answer")
        if not isinstance(answer, (str, dict, list, int, float, bool, type(None))): raise TreeHouseError("Unsupported submission answer", code="invalid_answer")
        state["submissions"][key] = {
            "id": key, "assignmentId": assignment_id, "courseId": assignment["courseId"], "profileId": actor_id,
            "answer": copy.deepcopy(answer), "status": "submitted", "attempts": attempts,
            "submittedAt": at, "updatedAt": at, "grade": None, "feedback": "",
        }
        event = emit("submission.submitted", actor_id, "submission", key, {"assignmentId": assignment_id, "attempt": attempts})
        return {"submissionId": key, "eventId": event["id"], "attempt": attempts}

    if kind == "submission.grade":
        submission_id = _id(payload.get("submissionId"), "submissionId")
        submission = _entity(state, "submissions", submission_id, "Submission")
        assignment = _entity(state, "assignments", submission["assignmentId"], "Assignment"); _course_author(state, actor_id, assignment["courseId"])
        score = _integer(payload.get("score"), "score", maximum=assignment["maxPoints"])
        feedback = _text(payload.get("feedback"), "feedback", maximum=16_384, required=False)
        percent = round(score / assignment["maxPoints"] * 100, 2)
        submission.update({"status": "graded", "grade": score, "feedback": feedback, "gradedAt": at, "gradedBy": actor_id, "updatedAt": at})
        skills = [{"skillId": item, "points": round(score / max(1, len(assignment.get("skillIds", []))))} for item in assignment.get("skillIds", [])]
        event = emit("submission.graded", submission["profileId"], "submission", submission_id, {"assignmentId": assignment["id"], "score": score, "maxPoints": assignment["maxPoints"], "percent": percent, "points": score, "skills": skills, "feedback": feedback})
        sync_course_completion(assignment["courseId"], submission["profileId"])
        return {"submissionId": submission_id, "eventId": event["id"], "percent": percent}

    if kind in {"skill.create", "skill.update"}:
        _require_role(state, actor_id, "admin", "instructor")
        if kind == "skill.create":
            skill_id = _id(payload.get("id") or _new_id("skill"), "skillId")
            if skill_id in state["skills"]: raise TreeHouseError("Skill already exists", code="already_exists", status=409)
            prerequisites = _list_ids(payload.get("prerequisiteIds"), "prerequisiteIds")
            if any(item not in state["skills"] for item in prerequisites): raise TreeHouseError("Prerequisite skill not found", code="skill_not_found")
            state["skills"][skill_id] = {
                "id": skill_id, "title": _text(payload.get("title"), "title", maximum=256),
                "description": _text(payload.get("description"), "description", maximum=16_384, required=False),
                "prerequisiteIds": prerequisites, "thresholds": [0, 25, 60, 100],
                "masteryThreshold": _integer(payload.get("masteryThreshold", 60), "masteryThreshold", maximum=100_000),
                "evidencePoints": _integer(payload.get("evidencePoints", 25), "evidencePoints", maximum=10_000),
                "createdAt": at, "updatedAt": at,
            }
            event_type = "skill.created"
        else:
            skill_id = _id(payload.get("skillId"), "skillId"); skill = _entity(state, "skills", skill_id, "Skill")
            if "title" in payload: skill["title"] = _text(payload["title"], "title", maximum=256)
            if "description" in payload: skill["description"] = _text(payload["description"], "description", maximum=16_384, required=False)
            if "prerequisiteIds" in payload:
                prerequisites = _list_ids(payload["prerequisiteIds"], "prerequisiteIds")
                if skill_id in prerequisites or any(item not in state["skills"] for item in prerequisites): raise TreeHouseError("Invalid prerequisites", code="skill_not_found")
                skill["prerequisiteIds"] = prerequisites
            skill["updatedAt"] = at; event_type = "skill.updated"
        cycle = _skill_cycle(state["skills"])
        if cycle: raise TreeHouseError("Skill prerequisites contain a cycle", code="prerequisite_cycle", status=409, details={"cycle": cycle})
        emit(event_type, actor_id, "skill", skill_id, {})
        return {"skillId": skill_id}

    if kind == "evidence.submit":
        _require_role(state, actor_id, "learner")
        skill_id = _id(payload.get("skillId"), "skillId"); _entity(state, "skills", skill_id, "Skill")
        _require_skills_unlocked(state, actor_id, [skill_id])
        evidence_id = _id(payload.get("id") or _new_id("evidence"), "evidenceId")
        state["evidence"][evidence_id] = {"id": evidence_id, "skillId": skill_id, "profileId": actor_id, "description": _text(payload.get("description"), "description", maximum=32_768), "sourceUrl": _text(payload.get("sourceUrl"), "sourceUrl", maximum=2_048, required=False), "status": "pending", "createdAt": at, "updatedAt": at}
        emit("evidence.submitted", actor_id, "evidence", evidence_id, {"skillId": skill_id})
        return {"evidenceId": evidence_id}

    if kind == "evidence.review":
        _require_role(state, actor_id, "admin", "instructor")
        evidence_id = _id(payload.get("evidenceId"), "evidenceId"); evidence = _entity(state, "evidence", evidence_id, "Evidence")
        decision = str(payload.get("decision") or "")
        if decision not in {"approved", "rejected"}: raise TreeHouseError("Evidence decision must be approved or rejected", code="invalid_decision")
        evidence.update({"status": decision, "reviewedBy": actor_id, "reviewedAt": at, "reviewNote": _text(payload.get("note"), "note", maximum=8_192, required=False), "updatedAt": at})
        data = {"skillId": evidence["skillId"]}
        if decision == "approved":
            points = state["skills"][evidence["skillId"]]["evidencePoints"]
            data.update({"points": points, "skills": [{"skillId": evidence["skillId"], "points": points}]})
        event = emit(f"evidence.{decision}", evidence["profileId"], "evidence", evidence_id, data)
        return {"evidenceId": evidence_id, "eventId": event["id"], "status": decision}

    if kind == "badge.create":
        _require_role(state, actor_id, "admin", "instructor")
        badge_id = _id(payload.get("id") or _new_id("badge"), "badgeId")
        criteria = payload.get("criteria") or {}
        if not isinstance(criteria, dict) or criteria.get("type") not in {"points", "skill", "course", "quest"}: raise TreeHouseError("Invalid badge criteria", code="invalid_criteria")
        criteria = copy.deepcopy(criteria); criteria_type = criteria["type"]
        if criteria_type == "points": criteria["threshold"] = _integer(criteria.get("threshold"), "threshold", maximum=10_000_000)
        elif criteria_type == "skill":
            criteria["skillId"] = _id(criteria.get("skillId"), "skillId"); _entity(state, "skills", criteria["skillId"], "Skill")
            criteria["threshold"] = _integer(criteria.get("threshold", 60), "threshold", maximum=1_000_000)
        elif criteria_type == "course":
            criteria["courseId"] = _id(criteria.get("courseId"), "courseId"); _entity(state, "courses", criteria["courseId"], "Course")
        else:
            criteria["questId"] = _id(criteria.get("questId"), "questId"); _entity(state, "quests", criteria["questId"], "Quest")
        state["badges"][badge_id] = {"id": badge_id, "title": _text(payload.get("title"), "title", maximum=256), "description": _text(payload.get("description"), "description", maximum=8_192, required=False), "criteria": copy.deepcopy(criteria), "createdAt": at}
        emit("badge.created", actor_id, "badge", badge_id, {"criteria": criteria})
        return {"badgeId": badge_id}

    if kind == "quest.create":
        _require_role(state, actor_id, "admin", "instructor")
        quest_id = _id(payload.get("id") or _new_id("quest"), "questId")
        activity_ids = _list_ids(payload.get("activityIds"), "activityIds")
        assignment_ids = _list_ids(payload.get("assignmentIds"), "assignmentIds")
        if not activity_ids and not assignment_ids: raise TreeHouseError("Quest needs an activity or assignment", code="empty_quest")
        if any(item not in state["activities"] for item in activity_ids) or any(item not in state["assignments"] for item in assignment_ids): raise TreeHouseError("Quest references missing work", code="entity_not_found")
        state["quests"][quest_id] = {"id": quest_id, "title": _text(payload.get("title"), "title", maximum=256), "description": _text(payload.get("description"), "description", maximum=8_192, required=False), "activityIds": activity_ids, "assignmentIds": assignment_ids, "rewardPoints": _integer(payload.get("rewardPoints", 25), "rewardPoints", maximum=10_000), "status": "active", "createdAt": at}
        emit("quest.created", actor_id, "quest", quest_id, {})
        return {"questId": quest_id}

    raise TreeHouseError(f"Unsupported TreeHouse command: {kind}", code="unsupported_command", status=400)


def apply_treehouse_command(
    state: dict[str, Any],
    command: dict[str, Any],
    *,
    actor_id: str,
    command_id: str,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    validate_treehouse_state(state)
    actor_id = _id(actor_id, "actorId")
    command_id = _id(command_id, "commandId")
    replay = state["processedCommands"].get(command_id)
    if replay:
        return state, {**copy.deepcopy(replay["result"]), "replayed": True}, False
    if expected_revision is not None and int(expected_revision) != int(state["revision"]):
        raise TreeHouseError("TreeHouse state changed in another tab", code="stale", status=409, details={"revision": state["revision"]})
    _profile(state, actor_id)
    next_state = copy.deepcopy(state)
    timestamp = _now(now)
    result = _command_result(next_state, command, actor_id, command_id, timestamp)
    next_state["revision"] += 1
    next_state["updatedAt"] = timestamp
    next_state["processedCommands"][command_id] = {"result": copy.deepcopy(result), "revision": next_state["revision"], "at": timestamp}
    while len(next_state["processedCommands"]) > MAX_PROCESSED_COMMANDS:
        next_state["processedCommands"].pop(next(iter(next_state["processedCommands"])))
    return next_state, {**result, "revision": next_state["revision"], "replayed": False}, True


def public_treehouse_snapshot(state: dict[str, Any], actor_id: str) -> dict[str, Any]:
    validate_treehouse_state(state)
    actor = _profile(state, actor_id)
    projection = compute_treehouse_projections(state)
    is_staff = _has_role(actor, "admin", "instructor")
    snapshot = copy.deepcopy(state)
    snapshot.pop("processedCommands", None)
    if not is_staff:
        snapshot["courses"] = {key: value for key, value in snapshot["courses"].items() if value.get("status") == "published" or actor_id in value.get("authorIds", [])}
        visible_courses = set(snapshot["courses"])
        for collection in ("modules", "activities", "assignments"):
            snapshot[collection] = {key: value for key, value in snapshot[collection].items() if value.get("courseId") in visible_courses and (collection != "assignments" or value.get("status") == "published")}
        snapshot["submissions"] = {key: value for key, value in snapshot["submissions"].items() if value.get("profileId") == actor_id}
        snapshot["evidence"] = {key: value for key, value in snapshot["evidence"].items() if value.get("profileId") == actor_id}
        snapshot["events"] = [event for event in snapshot["events"] if event.get("subjectId") == actor_id or event.get("type") in {"course.published", "skill.created", "badge.created", "quest.created"}]
        projection = {"learners": {actor_id: projection["learners"].get(actor_id, {})}, "leaderboard": [], "courses": {}, "eventCount": len(snapshot["events"])}
    permissions = {
        "admin": _has_role(actor, "admin"),
        "author": _has_role(actor, "admin", "instructor"),
        "learner": _has_role(actor, "learner"),
        "grade": _has_role(actor, "admin", "instructor"),
        "analytics": _has_role(actor, "admin", "instructor"),
    }
    return {"state": snapshot, "projection": projection, "actor": copy.deepcopy(actor), "permissions": permissions}


def plan_legacy_migration(documents: Iterable[dict[str, Any]], state: dict[str, Any]) -> dict[str, Any]:
    validate_treehouse_state(state)
    known = state.get("migrations", {}).get("legacyFrontmatter", {}).get("sources", {})
    candidates = []
    for doc in documents:
        if doc.get("kind") in {"treehouse-state", "calendar-projection", "planning", "asset", "base"}:
            continue
        treehouse = doc.get("treehouse") or {}
        frontmatter = doc.get("frontmatter") or {}
        if not (treehouse or doc.get("kind", "").startswith("treehouse-") or frontmatter.get("course") or frontmatter.get("skill")):
            continue
        source_key = f"{doc.get('id')}:{doc.get('head')}"
        if source_key in known:
            continue
        candidates.append({
            "sourceKey": source_key,
            "documentId": doc.get("id"),
            "head": doc.get("head"),
            "name": doc.get("name"),
            "course": treehouse.get("course") or frontmatter.get("course"),
            "skill": treehouse.get("skill") or frontmatter.get("skill"),
            "prerequisite": treehouse.get("prerequisite") or frontmatter.get("depends_on"),
            "text": doc.get("text") or "",
            "tasks": doc.get("tasks") or [],
        })
    return {"schemaVersion": 1, "candidates": candidates, "counts": {"documents": len(candidates), "courses": len({item["course"] for item in candidates if item["course"]}), "skills": len({item["skill"] for item in candidates if item["skill"]}), "tasks": sum(len(item["tasks"]) for item in candidates)}}


def apply_legacy_migration(
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    actor_id: str,
    command_id: str,
    expected_revision: int | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    validate_treehouse_state(state)
    _require_role(state, actor_id, "admin", "instructor")
    if command_id in state["processedCommands"]:
        replay = state["processedCommands"][command_id]["result"]
        return state, {**copy.deepcopy(replay), "replayed": True}, False
    if expected_revision is not None and expected_revision != state["revision"]:
        raise TreeHouseError("TreeHouse state changed in another tab", code="stale", status=409, details={"revision": state["revision"]})
    next_state = copy.deepcopy(state); timestamp = _now(now)
    sources = next_state["migrations"]["legacyFrontmatter"]["sources"]
    course_ids: dict[str, str] = {}
    skill_ids: dict[str, str] = {}
    imported = {"courses": 0, "modules": 0, "activities": 0, "assignments": 0, "skills": 0, "documents": 0}

    for item in plan.get("candidates", []):
        if item["sourceKey"] in sources: continue
        course_name = str(item.get("course") or "").strip()
        if course_name:
            course_id = course_ids.setdefault(course_name, str(uuid.uuid5(LEGACY_NAMESPACE, f"course\n{course_name}")))
            if course_id not in next_state["courses"]:
                next_state["courses"][course_id] = {"id": course_id, "title": course_name, "description": "Imported from legacy TreeHouse frontmatter", "tags": ["legacy-import"], "status": "draft", "authorIds": [actor_id], "moduleIds": [], "createdAt": timestamp, "updatedAt": timestamp}
                imported["courses"] += 1
            module_id = str(uuid.uuid5(LEGACY_NAMESPACE, f"module\n{course_name}"))
            if module_id not in next_state["modules"]:
                next_state["modules"][module_id] = {"id": module_id, "courseId": course_id, "title": "Imported lessons", "description": "", "activityIds": [], "assignmentIds": [], "order": 0, "createdAt": timestamp, "updatedAt": timestamp}
                next_state["courses"][course_id]["moduleIds"].append(module_id); imported["modules"] += 1
            activity_id = str(uuid.uuid5(LEGACY_NAMESPACE, f"activity\n{item['documentId']}"))
            if activity_id not in next_state["activities"]:
                next_state["activities"][activity_id] = {"id": activity_id, "courseId": course_id, "moduleId": module_id, "title": str(item.get("name") or "Imported lesson"), "content": item.get("text") or "", "activityType": "markdown", "status": "published", "points": 10, "skillIds": [], "order": len(next_state["modules"][module_id]["activityIds"]), "sourceDocumentId": item["documentId"], "sourceHead": item["head"], "createdAt": timestamp, "updatedAt": timestamp}
                next_state["modules"][module_id]["activityIds"].append(activity_id); imported["activities"] += 1
            for task in item.get("tasks", []):
                assignment_id = str(uuid.uuid5(LEGACY_NAMESPACE, f"assignment\n{item['documentId']}\n{task.get('id')}"))
                if assignment_id not in next_state["assignments"]:
                    next_state["assignments"][assignment_id] = {"id": assignment_id, "courseId": course_id, "moduleId": module_id, "title": str(task.get("text") or "Imported evidence task"), "prompt": f"Imported from {item.get('name')}", "status": "draft", "dueAt": "", "maxPoints": 100, "skillIds": [], "allowRetries": True, "maxAttempts": 0, "sourceDocumentId": item["documentId"], "sourceTaskId": task.get("id"), "createdAt": timestamp, "updatedAt": timestamp}
                    next_state["modules"][module_id]["assignmentIds"].append(assignment_id); imported["assignments"] += 1
        skill_name = str(item.get("skill") or "").strip()
        if skill_name:
            skill_id = skill_ids.setdefault(skill_name, str(uuid.uuid5(LEGACY_NAMESPACE, f"skill\n{skill_name}")))
            if skill_id not in next_state["skills"]:
                next_state["skills"][skill_id] = {"id": skill_id, "title": skill_name, "description": f"Imported from {item.get('name')}", "prerequisiteIds": [], "thresholds": [0, 25, 60, 100], "masteryThreshold": 60, "evidencePoints": 25, "sourceDocumentId": item["documentId"], "sourceHead": item["head"], "createdAt": timestamp, "updatedAt": timestamp}
                imported["skills"] += 1
        sources[item["sourceKey"]] = {"documentId": item["documentId"], "head": item["head"], "importedAt": timestamp}
        imported["documents"] += 1

    # Resolve prerequisite names after all skills exist. Unknown names remain a
    # migration diagnostic instead of inventing a node.
    title_to_id = {skill["title"].casefold(): skill_id for skill_id, skill in next_state["skills"].items()}
    unresolved = []
    for item in plan.get("candidates", []):
        skill_name = str(item.get("skill") or "").strip(); dependency = str(item.get("prerequisite") or "").strip()
        if not skill_name or not dependency: continue
        skill_id = title_to_id.get(skill_name.casefold()); dependency_id = title_to_id.get(dependency.casefold())
        if skill_id and dependency_id and dependency_id != skill_id:
            next_state["skills"][skill_id]["prerequisiteIds"] = list(dict.fromkeys([*next_state["skills"][skill_id]["prerequisiteIds"], dependency_id]))
        elif not dependency_id:
            unresolved.append({"skill": skill_name, "prerequisite": dependency})
    cycle = _skill_cycle(next_state["skills"])
    if cycle: raise TreeHouseError("Legacy migration would create a prerequisite cycle", code="prerequisite_cycle", status=409, details={"cycle": cycle})

    next_state["events"].append({"id": str(uuid.uuid5(EVENT_NAMESPACE, f"{command_id}\nmigration")), "commandId": command_id, "type": "migration.legacy_applied", "actorId": actor_id, "subjectId": actor_id, "entityType": "migration", "entityId": "legacy-frontmatter", "at": timestamp, "data": {"imported": imported, "unresolved": unresolved}})
    next_state["revision"] += 1; next_state["updatedAt"] = timestamp
    result = {"imported": imported, "unresolvedPrerequisites": unresolved, "revision": next_state["revision"], "replayed": False}
    next_state["processedCommands"][command_id] = {"result": copy.deepcopy(result), "revision": next_state["revision"], "at": timestamp}
    return next_state, result, True


def state_fingerprint(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()

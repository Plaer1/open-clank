"""Native rich learning definitions; no vendor runtime or arbitrary host execution."""
from __future__ import annotations
import copy
import json
import math
from src.openclank.copal_treehouse import TreeHouseError


def invalid(message):
    raise TreeHouseError(message, code="invalid_learning_adapter", status=400)


def bounded_text(value, label, limit=240, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        invalid(f"{label} must be text of at most {limit} characters")
    return value


def validate_adapter(value, activity_type):
    if value is None: return {}
    if not isinstance(value, dict) or len(json.dumps(value)) > 100000: invalid("Invalid adapter definition")
    value = copy.deepcopy(value)
    allowed = {"captionOperationId", "captionLanguage", "captionLabel"}
    if activity_type == "code":
        allowed |= {"language", "source", "expectedOutput"}
        if value.get("language", "javascript") != "javascript": invalid("Only browser JavaScript execution is supported")
        value["language"] = "javascript"
        value["source"] = bounded_text(value.get("source", "return 2 + 2;"), "JavaScript source", 16000)
        if "expectedOutput" in value: bounded_text(value["expectedOutput"], "Expected JSON output", 8000, True)
    if activity_type == "interactive":
        allowed |= {"prompt", "choices", "answerIndex", "feedback"}
        value["prompt"] = bounded_text(value.get("prompt"), "Prompt", 4000)
        choices = value.get("choices")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 12: invalid("Interactive content needs 2–12 choices")
        value["choices"] = [bounded_text(x, "Choice", 1000) for x in choices]
        answer = value.get("answerIndex")
        if type(answer) is not int or not 0 <= answer < len(choices): invalid("Answer index is outside the choices")
        bounded_text(value.get("feedback", ""), "Feedback", 4000, True)
    if set(value) - allowed: invalid("Unsupported adapter field")
    for key in value.keys() & {"captionOperationId", "captionLanguage", "captionLabel"}:
        bounded_text(value[key], key, 240)
    return value


def validate_podcast(value, state, course_id):
    if not isinstance(value, dict) or set(value) - {"title", "description", "published", "episodes"}: invalid("Invalid podcast definition")
    result = {"title": bounded_text(value.get("title"), "Podcast title"),
              "description": bounded_text(value.get("description", ""), "Description", 4000, True),
              "published": value.get("published", False), "episodes": []}
    if type(result["published"]) is not bool: invalid("Published must be boolean")
    episodes = value.get("episodes", [])
    if not isinstance(episodes, list) or len(episodes) > 200: invalid("Podcast supports at most 200 ordered episodes")
    seen = set()
    for row in episodes:
        if not isinstance(row, dict) or set(row) - {"activityId", "operationId", "title", "description"}: invalid("Invalid episode")
        aid, oid = row.get("activityId"), row.get("operationId")
        activity = state.get("activities", {}).get(aid, {})
        if activity.get("courseId") != course_id or activity.get("deletedAt") or activity.get("activityType") != "audio": invalid("Episodes require a course audio activity")
        if result["published"] and activity.get("status") != "published": invalid("Published podcasts require published audio activities")
        if not any(x.get("operationId") == oid for x in activity.get("sourceAttachments", [])): invalid("Episode requires a prepared Files audio source")
        if (aid, oid) in seen: invalid("Duplicate podcast episode")
        seen.add((aid, oid))
        result["episodes"].append({"activityId": aid, "operationId": oid,
            "title": bounded_text(row.get("title", activity.get("title")), "Episode title"),
            "description": bounded_text(row.get("description", ""), "Episode description", 2000, True)})
    if result["published"] and not episodes: invalid("Publishing needs at least one episode")
    return result


def validate_position(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 864000:
        invalid("Playback position must be finite seconds between 0 and 864000")
    return round(value, 3)


def export_podcasts(state, course_ids):
    return {cid: {"title": value["title"], "description": value["description"], "published": value["published"],
                  "episodes": [{key: ep.get(key, "") for key in ("activityId", "title", "description")} for ep in value["episodes"]]}
            for cid, value in state.get("extensions", {}).get("richLearning", {}).get("podcasts", {}).items() if cid in course_ids}


def import_podcasts(value, state, mapping):
    if value is None: return
    if not isinstance(value, dict) or set(value) - {"podcasts"}: invalid("Unsupported portable rich learning definition")
    rows = value.get("podcasts", {})
    if not isinstance(rows, dict) or len(rows) > 2000: invalid("Invalid portable podcasts")
    for cid, podcast in rows.items():
        if cid not in mapping["courses"] or not isinstance(podcast, dict) or set(podcast) - {"title", "description", "published", "episodes"}: invalid("Podcast references missing course")
        episodes = podcast.get("episodes")
        if not isinstance(episodes, list) or len(episodes) > 200: invalid("Invalid portable episodes")
        target = mapping["courses"][cid]
        imported = {"title": bounded_text(podcast.get("title"), "Podcast title"),
                    "description": bounded_text(podcast.get("description", ""), "Description", 4000, True),
                    "published": False, "episodes": []}
        seen = set()
        for ep in episodes:
            if not isinstance(ep, dict) or set(ep) - {"activityId", "title", "description"}: invalid("Invalid portable episode")
            aid = mapping["activities"].get(ep.get("activityId"))
            activity = state.get("activities", {}).get(aid, {})
            if not aid or aid in seen or activity.get("courseId") != target or activity.get("activityType") != "audio": invalid("Podcast episode references invalid audio activity")
            seen.add(aid)
            imported["episodes"].append({"activityId": aid, "operationId": "", "title": bounded_text(ep.get("title"), "Episode title"),
                                         "description": bounded_text(ep.get("description", ""), "Episode description", 2000, True)})
        state.setdefault("extensions", {}).setdefault("richLearning", {}).setdefault("podcasts", {})[target] = imported


FORMAT_SUPPORT = {
    "native": {"version": "copal-treehouse-course-v1", "available": True,
        "behavior": "Curriculum JSON import/export; ordering and adapters preserved; Files resources require fresh authorization/preparation. No private progress or grants."},
    "interactive": {"version": "openclank-choice-v1", "available": True, "rights": "Native implementation; no upstream code copied", "behavior": "Declarative local choice activity with feedback; practice result is not trusted assessment evidence."},
    "code": {"version": "browser-javascript-v1", "available": True, "behavior": "Opaque sandbox frame + dedicated Worker; 2 second deadline, 16000 character source, 8000 character output. No network; no host execution. Browser memory cannot be hard capped; result is practice only."},
    "h5p": {"version": "H5P 1.x packages", "available": False, "rights": "H5P uses MIT where possible; h5p-php-library declares GPL-3.0 due to HTML purifier dependencies. Each runtime, content library and asset requires independent license/version review.", "reason": "No pinned H5P runtime/content-library dependency, asset package validator or authorized package transport. Raw ZIP/HTML is not executed or falsely imported."},
    "scorm12": {"version": "SCORM 1.2", "available": False, "rights": "LearnHouse 1.3.7 SCORM runtime is under enterprise source; no permission to copy it inferred", "reason": "No first-party ZIP/manifest validator, isolated package asset host or SCORM LMS runtime/resume contract."},
    "scorm2004": {"version": "SCORM 2004 (all editions)", "available": False, "rights": "No enterprise implementation copied", "reason": "SCORM 2004 sequencing/navigation, runtime data model and isolated asset hosting are unimplemented."},
    "automation": {"version": "existing-treehouse-events", "available": False, "reason": "No learning-specific scheduler subscription/replay/cancel adapter; external webhooks are not created implicitly."},
}

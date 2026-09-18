"""Versioned Open Clank Field Guide template.

The template is data, rather than a home-directory seed side effect.  A
caller can instantiate it into an account-owned TreeHouse catalogue and use
the stable keys for upgrades without replacing authored drafts.
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any


FIELD_GUIDE_TEMPLATE_KEY = "open-clank-field-guide"
FIELD_GUIDE_TEMPLATE_VERSION = "2026-09-05.1"
FIELD_GUIDE_COVERAGE_VERSION = "2026-09-08.1"

_COURSES = (
    ("fg-orientation", "Find your footing", "fg-first-footing", "Editor, Files and Timeline"),
    ("fg-assistant", "Work with your clanker", "fg-clank-collaborator", "Assistant, sessions and tool activity"),
    ("fg-editor", "Make yourself at home in Editor", "fg-document-pilot", "Editor and Markdown"),
    ("fg-files", "Keep your files connected", "fg-file-navigator", "Files and attachments"),
    ("fg-wiki", "Build a small Wiki", "fg-wiki-gardener", "Wiki and linked story cards"),
    ("fg-bases", "Give your notes useful structure", "fg-pattern-builder", "Bases and typed properties"),
    ("fg-timeline", "Read and shape a Timeline", "fg-timekeeper", "Timeline tracks and events"),
    ("fg-connections", "Explore connections in Graph and Mind", "fg-connection-finder", "Graph, Galaxy and Mind"),
    ("fg-tasks", "Turn notes into action", "fg-follow-through", "Tasks and source checkboxes"),
    ("fg-settings", "Make the workspace fit", "fg-workspace-steward", "Settings, appearance and preferences"),
    ("fg-continuity", "Understand memory and recovery", "fg-continuity-keeper", "Memory, backup and recovery"),
    ("fg-teaching", "Teach what you learned", "fg-trail-maker", "TreeHouse authoring (elective)"),
    ("fg-models", "Choose and compare your tools", "fg-tool-scout", "Models, providers and Compare"),
    ("fg-automation", "Put your clanker on a schedule", "fg-steady-operator", "Tasks, goals and automation"),
    ("fg-research-media", "Investigate and create", "fg-evidence-maker", "Research, gallery and generated media"),
    ("fg-communications", "Keep conversations and dates connected", "fg-connected-planner", "Email, calendar and webhooks"),
    ("fg-operations", "Care for the installation", "fg-system-caretaker", "Diagnostics, security and operations (elective)"),
)

# These locators are the shipped entry points, rather than prose labels.  A
# lesson may describe an optional provider-backed action, but it must still
# land on a real surface where the disposable exercise can be inspected.
_SURFACE_DETAILS = {
    "fg-orientation": {"key": "treehouse", "label": "TreeHouse", "href": "/copal/treehouse", "locator": "[data-copal-view=treehouse]"},
    "fg-assistant": {"key": "assistant", "label": "Assistant", "href": "/", "locator": "#message"},
    "fg-editor": {"key": "editor", "label": "Editor", "href": "/copal/editor", "locator": "[data-copal-view=notes]"},
    "fg-files": {"key": "files", "label": "Files", "href": "/files", "locator": "[data-files-launcher]"},
    "fg-wiki": {"key": "wiki", "label": "Wiki", "href": "/copal/wiki", "locator": "[data-copal-view=wiki]"},
    "fg-bases": {"key": "bases", "label": "Bases in Editor", "href": "/copal/bases", "locator": "[data-copal-view=notes]"},
    "fg-timeline": {"key": "timeline", "label": "Timeline", "href": "/copal/timeline", "locator": "[data-copal-view=timeline]"},
    "fg-connections": {"key": "graph", "label": "Graph and Mind", "href": "/copal/graph", "locator": "[data-copal-view=graph]"},
    "fg-tasks": {"key": "tasks", "label": "Meatbag Tasks", "href": "/copal/todo", "locator": "[data-copal-view=todo]"},
    "fg-settings": {"key": "settings", "label": "Settings", "href": "/settings", "locator": "#rail-settings"},
    "fg-continuity": {"key": "continuity", "label": "Memory and history", "href": "/", "locator": "#memory-mode-select"},
    "fg-teaching": {"key": "teaching", "label": "TreeHouse Admin", "href": "/copal/treehouse", "locator": "[data-copal-view=treehouse]"},
    "fg-models": {"key": "models", "label": "Models and Compare", "href": "/settings", "locator": "#rail-settings"},
    "fg-automation": {"key": "automation", "label": "Clanker Tasks", "href": "/", "locator": "#tool-tasks-btn"},
    "fg-research-media": {"key": "research", "label": "Research and Gallery", "href": "/", "locator": "#rail-research"},
    "fg-communications": {"key": "communications", "label": "Email and Calendar", "href": "/", "locator": "#rail-email"},
    "fg-operations": {"key": "operations", "label": "Operations settings", "href": "/settings", "locator": "#rail-settings"},
}

_PRACTICE_DEFAULTS = {
    "fg-orientation": ("Field Guide map", "Identify Editor, Files, Timeline and the current TreeHouse course.", "The learner can name all four destinations and return to this lesson."),
    "fg-assistant": ("Practice brief", "Ask for a two-step summary of this disposable brief and save the response as a session.", "The session contains the brief title and two numbered steps."),
    "fg-editor": ("Disposable note", "# Field Guide note\n\n- [ ] One practice check", "The note reopens with its heading and checked-list syntax."),
    "fg-files": ("Disposable file", "field-guide-practice.txt\nKeep this file disposable.", "Files shows the renamed practice path."),
    "fg-wiki": ("Practice card", "A short card linked to the Field Guide note.", "The card reopens and its source link resolves."),
    "fg-bases": ("Practice row", "status: ready\ntopic: field-guide", "The typed status value is returned by the Base query."),
    "fg-timeline": ("Practice event", "One single-day event on the Field Guide practice track.", "Reloading Timeline preserves the track and event date."),
    "fg-connections": ("Practice relation", "Link the practice note and card; remove only this relation.", "Graph and Mind show the link before cleanup and not after."),
    "fg-tasks": ("Practice checkbox", "- [ ] Complete this disposable task", "Completing the task changes the source checkbox."),
    "fg-settings": ("Workspace preference", "A reversible display preference in the current workspace.", "Reloading keeps the preference in this workspace only."),
    "fg-continuity": ("Practice checkpoint", "A checkpoint for the disposable Field Guide note.", "The checkpoint id and retained state are visible in history."),
    "fg-teaching": ("Draft lesson", "A learner instruction with one observable practice requirement.", "Preview shows the instruction before the author publishes it."),
    "fg-models": ("Comparison prompt", "Compare two recorded fixture responses; live inference is optional.", "The selected response and reason are recorded without provider credentials."),
    "fg-automation": ("Disabled reminder", "A one-shot reminder fixture that is disabled before its run time.", "The schedule shows its payload and cancelled status."),
    "fg-research-media": ("Evidence packet", "A supplied source plus one reversible practice image edit.", "The image retains its source reference after reopening."),
    "fg-communications": ("Communication draft", "A local email/calendar placeholder; no external send.", "The draft and calendar placeholder remain local and inspectable."),
    "fg-operations": ("Safe diagnostics", "A read-only diagnostics and export fixture.", "The export manifest contains only the disposable practice project."),
}

_ACHIEVEMENTS = (
    ("fg-first-circuit", "First Circuit", ("fg-first-footing", "fg-clank-collaborator", "fg-document-pilot")),
    ("fg-connected-researcher", "Connected Researcher", ("fg-file-navigator", "fg-wiki-gardener", "fg-connection-finder")),
    ("fg-plan-into-practice", "Plan into Practice", ("fg-pattern-builder", "fg-timekeeper", "fg-follow-through")),
    ("fg-comfortable-controls", "Comfortable at the Controls", ("fg-document-pilot", "fg-workspace-steward")),
    ("fg-context-keeper", "Context Keeper", ("fg-clank-collaborator", "fg-continuity-keeper")),
    ("fg-graduate", "Field Guide Graduate", tuple(item[2] for item in _COURSES if item[0] not in {"fg-teaching", "fg-operations"})),
)

_LESSON_DETAILS = {
    "fg-orientation": ("Create a disposable note in Editor; open it from Files; add one dated Timeline event; reload and confirm all three views show the same saved title.", "editor_files_timeline_saved", True, ["editor.write", "files.read", "timeline.write"]),
    "fg-assistant": ("Ask the Assistant for a two-step plan; save the plan as a session; resume the session and verify the second step remains.", "assistant_session_resume", True, ["assistant.use", "sessions.write"]),
    "fg-editor": ("Edit the supplied Markdown fixture; add a heading and one checklist item; save, reopen, and inspect the revision marker.", "editor_markdown_revision", True, ["editor.write"]),
    "fg-files": ("Open the supplied practice folder; create one disposable text file; rename it; verify the final path from Files.", "files_path_round_trip", True, ["files.write"]),
    "fg-wiki": ("Create a Wiki card from the fixture; link it to the supplied note; reopen the link and verify both titles.", "wiki_link_round_trip", True, ["wiki.write", "graph.write"]),
    "fg-bases": ("Open the supplied Base; add a typed status value to its practice row; run the view and verify the row is returned.", "base_typed_query", True, ["bases.write", "bases.query"]),
    "fg-timeline": ("Create a practice track; place one event on it; move the event once; reload Timeline and verify its parent track.", "timeline_track_event", True, ["timeline.write"]),
    "fg-connections": ("Link the two supplied cards; inspect Graph; open the same relation in Mind and Galaxy; remove only the practice link.", "connection_link_cleanup", True, ["graph.write", "mind.read", "galaxy.read"]),
    "fg-tasks": ("Add a task to the fixture note; complete it in Tasks; reopen the source note and verify its checkbox is checked.", "task_source_checkbox", True, ["tasks.write", "editor.write"]),
    "fg-settings": ("Change one workspace preference; reload Settings; restore the original value and verify the preference is scoped to this workspace.", "workspace_preference_round_trip", True, ["settings.write"]),
    "fg-continuity": ("Create a checkpoint from the practice note; inspect its recovery entry; restore the fixture only after recording the checkpoint id.", "checkpoint_recovery_record", True, ["memory.write", "backup.read"]),
    "fg-teaching": ("Duplicate the practice lesson as a draft; add a learner instruction; preview it as Learner; publish only after the preview is correct.", "course_draft_preview", True, ["treehouse.author"]),
    "fg-models": ("Compare two available model entries on the practice prompt; record the selected model and the reason in the lesson evidence.", "model_comparison_evidence", True, ["models.read", "compare.read"]),
    "fg-automation": ("Create a one-shot practice reminder; inspect its scheduled payload; cancel it before it can run.", "automation_cancelled_reminder", True, ["tasks.write", "automation.write"]),
    "fg-research-media": ("Save one research source to the fixture; attach a generated or uploaded image; reopen the gallery item and verify its source reference.", "research_media_source", True, ["research.write", "gallery.write"]),
    "fg-communications": ("Draft a communication from the fixture; create a matching calendar placeholder; inspect the webhook delivery record without sending externally.", "communication_calendar_draft", True, ["email.draft", "calendar.write", "webhooks.read"]),
    "fg-operations": ("Run the safe diagnostics view; inspect the security status; export only the disposable fixture and verify the export manifest.", "safe_operations_export", True, ["diagnostics.read", "security.read", "export.write"]),
}


def field_guide_manifest() -> dict[str, Any]:
    """Return an immutable-by-convention publication manifest."""
    courses = []
    badges = []
    for key, title, badge_key, surface_label in _COURSES:
        elective = key in {"fg-teaching", "fg-operations"}
        lesson_id = f"{key}:lesson-1"
        steps, verifier, self_check, capabilities = _LESSON_DETAILS[key]
        surface = _SURFACE_DETAILS[key]
        practice_title, practice_seed, expected_evidence = _PRACTICE_DEFAULTS[key]
        position = [item[0] for item in _COURSES].index(key)
        courses.append({
            "key": key,
            "title": title,
            "description": f"Practice {surface_label} with disposable Field Guide material.",
            "elective": elective,
            "prerequisites": [] if position == 0 else (["fg-orientation"] if position == 1 else ["fg-orientation", _COURSES[position - 1][0]]),
            "lessons": [{
                "key": lesson_id,
                "title": f"{title}: first practice",
                "destination": surface_label,
                "surface": copy.deepcopy(_SURFACE_DETAILS[key]),
                "practiceFixture": f"field-guide/{key}",
                "practice": {"title": practice_title, "seed": practice_seed, "cleanup": "Delete or reset this disposable fixture after the check.", "expectedEvidence": expected_evidence},
                "verifier": verifier,
                "verifierSpec": {
                    "kind": verifier,
                    "evidence": expected_evidence,
                    "command": "node tests/treehouse_field_guide_browser_acceptance.mjs",
                    "runtime": "mounted-chromium",
                    "assertions": ["published-course-count", f"surface-entry:{surface['key']}", f"practice-seed:{key}", "cleanup-boundary"],
                },
                "selfCheck": self_check,
                "capabilityRequirements": capabilities,
                "steps": [steps],
                "content": steps,
            }],
        })
        badges.append({"key": badge_key, "label": title, "courseKey": key, "criteria": {"type": "course", "courseKey": key}})
    return {
        "templateKey": FIELD_GUIDE_TEMPLATE_KEY,
        "templateVersion": FIELD_GUIDE_TEMPLATE_VERSION,
        "title": "Open Clank Field Guide",
        "practiceProject": "Field Guide Practice",
        "courses": courses,
        "badges": badges,
        "achievements": [
            {"key": key, "label": label, "requiredBadges": list(required)}
            for key, label, required in _ACHIEVEMENTS
        ],
        "requiredCourseKeys": [item[0] for item in _COURSES if item[0] not in {"fg-teaching", "fg-operations"}],
        "coverage": {item[0]: item[3] for item in _COURSES},
        "coverageContract": {
            "version": FIELD_GUIDE_COVERAGE_VERSION,
            "kind": "disposable-mounted",
            "courseCount": len(_COURSES),
            "browserCommand": "node tests/treehouse_field_guide_browser_acceptance.mjs",
            "scope": "owner-account-and-workspace",
            "assertions": ["published-course-count", "surface-entry", "practice-seed", "verifier-evidence", "cleanup-boundary"],
        },
    }


def validate_field_guide_manifest(manifest: dict[str, Any] | None = None) -> None:
    """Reject publication metadata that cannot drive a disposable mounted exercise."""
    value = manifest or field_guide_manifest()
    expected = {item[0] for item in _COURSES}
    courses = value.get("courses") or []
    if len(courses) != len(expected):
        raise ValueError(f"Field Guide manifest must publish {len(expected)} courses")
    if {str(course.get("key") or "") for course in courses} != expected:
        raise ValueError("Field Guide manifest course keys do not match the canonical catalogue")
    contract = value.get("coverageContract") or {}
    if contract.get("version") != FIELD_GUIDE_COVERAGE_VERSION or contract.get("kind") != "disposable-mounted":
        raise ValueError("Field Guide manifest has no current disposable-mounted coverage contract")
    if contract.get("courseCount") != len(expected) or not contract.get("browserCommand"):
        raise ValueError("Field Guide coverage contract is missing its mounted command or course count")
    seen = set()
    lesson_ids = set()
    for course in value.get("courses", []):
        key = str(course.get("key") or "")
        seen.add(key)
        lessons = course.get("lessons") or []
        if len(lessons) != 1:
            raise ValueError(f"Field Guide course {key} must have one executable practice lesson")
        for lesson in course.get("lessons", []):
            lesson_key = str(lesson.get("key") or "")
            if not lesson_key or lesson_key in lesson_ids:
                raise ValueError(f"Field Guide lesson key is missing or duplicated: {lesson_key}")
            lesson_ids.add(lesson_key)
            surface = lesson.get("surface") or {}
            practice = lesson.get("practice") or {}
            if surface.get("key") not in {item.get("key") for item in _SURFACE_DETAILS.values()} or not surface.get("href") or not surface.get("locator"):
                raise ValueError(f"Field Guide lesson {lesson.get('key')} has no delivered surface locator")
            verifier = lesson.get("verifierSpec") or {}
            if not lesson.get("practiceFixture") or not practice.get("seed") or not practice.get("cleanup") or not verifier.get("evidence"):
                raise ValueError(f"Field Guide lesson {lesson.get('key')} has no disposable practice/verifier")
            if verifier.get("command") != "node tests/treehouse_field_guide_browser_acceptance.mjs" or verifier.get("runtime") != "mounted-chromium" or len(verifier.get("assertions") or []) < 3:
                raise ValueError(f"Field Guide lesson {lesson.get('key')} has no mounted verifier contract")
            if not lesson.get("steps") or not lesson.get("selfCheck"):
                raise ValueError(f"Field Guide lesson {lesson.get('key')} has no learner exercise steps or self-check")
    if seen != expected:
        raise ValueError(f"Field Guide manifest is missing courses: {sorted(expected - seen)}")


def instantiate_field_guide(state: dict[str, Any], owner_id: str) -> dict[str, Any]:
    """Install the template into a fresh owner state exactly once.

    Existing authored state is returned unchanged.  The route/repository owns
    durable idempotency and revision CAS; this helper only builds the content
    payload for that guarded commit.
    """
    result = copy.deepcopy(state)
    manifest = field_guide_manifest()
    validate_field_guide_manifest(manifest)
    result.setdefault("fieldGuide", {})
    if result["fieldGuide"].get("templateKey") == FIELD_GUIDE_TEMPLATE_KEY:
        return result
    # Keep command construction in this template adapter so the published
    # lesson graph is validated by the same domain state machine as authored
    # courses.  The import is local to avoid a module cycle at application
    # startup.
    from src.openclank.copal_treehouse import apply_treehouse_command
    owner_suffix = hashlib.sha256(str(owner_id).encode("utf-8")).hexdigest()[:12]

    profile = result.setdefault("profiles", {}).get(owner_id)
    if profile is None:
        source = result["profiles"].get("owner", {})
        result["profiles"][owner_id] = {
            "id": owner_id,
            "displayName": owner_id,
            "roles": ["admin", "instructor", "learner"],
            "active": True,
            "createdAt": source.get("createdAt"),
        }

    def run(kind: str, payload: dict[str, Any], suffix: str) -> None:
        nonlocal result
        result, _, _ = apply_treehouse_command(
            result,
            {"type": kind, "payload": payload},
            actor_id=owner_id,
            command_id=f"field-guide:{FIELD_GUIDE_TEMPLATE_VERSION}:{suffix}",
            expected_revision=result["revision"],
        )

    for course_spec in manifest["courses"]:
        course_key = course_spec["key"]
        # A template key is stable within the manifest, while repository
        # entities must also be unique across account catalogues.  The owner
        # suffix keeps two users' default Field Guides independently
        # addressable when one shares a course with the other.
        course_id = f"course:{course_key}:{owner_suffix}"
        module_id = f"module:{course_key}:{owner_suffix}"
        lesson = course_spec["lessons"][0]
        activity_id = f"activity:{lesson['key']}:{owner_suffix}"
        run("course.create", {"id": course_id, "title": course_spec["title"], "description": course_spec["description"], "tags": ["field-guide", course_key]}, f"course:{course_key}")
        result["courses"][course_id]["fieldGuideKey"] = course_key
        result["courses"][course_id]["elective"] = bool(course_spec["elective"])
        result["courses"][course_id]["prerequisites"] = [
            f"course:{prerequisite}:{owner_suffix}" for prerequisite in course_spec.get("prerequisites", [])
        ]
        run("module.create", {"id": module_id, "courseId": course_id, "title": "Practice", "description": "A disposable practice lesson."}, f"module:{course_key}")
        run("activity.create", {"id": activity_id, "moduleId": module_id, "title": lesson["title"], "activityType": "lesson", "content": lesson["content"], "points": 10, "skillIds": []}, f"lesson:{lesson['key']}")
        run("activity.update", {"activityId": activity_id, "status": "published"}, f"publish-lesson:{lesson['key']}")
        result["activities"][activity_id].update({
            "fieldGuideKey": lesson["key"],
            "destination": lesson["destination"],
            "practiceFixture": lesson["practiceFixture"],
            "verifier": lesson["verifier"],
            "selfCheck": lesson["selfCheck"],
            "capabilityRequirements": lesson["capabilityRequirements"],
            "steps": lesson.get("steps") or [],
            "surface": copy.deepcopy(lesson.get("surface") or {}),
            "practice": copy.deepcopy(lesson.get("practice") or {}),
            "verifierSpec": copy.deepcopy(lesson.get("verifierSpec") or {}),
        })
        run("course.publish", {"courseId": course_id}, f"publish-course:{course_key}")
        badge = next(item for item in manifest["badges"] if item["courseKey"] == course_key)
        run("badge.create", {"id": f"badge:{badge['key']}:{owner_suffix}", "title": badge["label"], "description": f"Complete {course_spec['title']}", "criteria": {"type": "course", "courseId": course_id}}, f"badge:{badge['key']}")
        result["badges"][f"badge:{badge['key']}:{owner_suffix}"]["fieldGuideKey"] = badge["key"]

    result["fieldGuide"] = {
        "templateKey": manifest["templateKey"],
        "templateVersion": manifest["templateVersion"],
        "requiredCourseKeys": manifest["requiredCourseKeys"],
        "achievements": manifest["achievements"],
        "coverage": manifest["coverage"],
        "ownerAccountId": owner_id,
    }
    result.setdefault("extensions", {})["fieldGuideManifest"] = manifest
    return result


__all__ = ["FIELD_GUIDE_TEMPLATE_KEY", "FIELD_GUIDE_TEMPLATE_VERSION", "field_guide_manifest", "validate_field_guide_manifest", "instantiate_field_guide"]
